"""argus-iris GPU leases around local-Ollama work (#51).

``with ollama_lease(model, kind=..., caller=..., endpoint=...) as grant:`` holds an
``ollama:<model>`` lease from the suite's GPU broker (argus-iris) for its body, and
``grant.endpoint`` is where to send the Ollama calls: the configured endpoint, unless the
broker placed the work on another node, then the same path on that node. iris decides
placement and what to evict; nothing here polls GPUs or other services.

Only :class:`auditor.llm_client.LLMClient` takes leases, and only when its provider is
local Ollama: a cloud provider never leases. The nightly repo pass holds one lease per
repo (``KIND_REPO``, taken at the repo's first model call, so an all-cached repo never
waits); any other Ollama call outside such a scope takes its own (``KIND_CALL``).

Off by default. ``ARGUS_GPU_COORDINATOR=broker`` turns it on, with argus-cortex's other
``ARGUS_GPU_*`` variables (``BROKER_URL``, ``BROKER_TOKEN``, ``TENANT``,
``TENANT_MANIFEST``, ``LEASE_TIMEOUT_S``). ``TENANT`` defaults to ``ai-code-auditor`` and
``TENANT_MANIFEST`` to the repo's ``gpu-tenant.yaml``. ``ARGUS_GPU_BROKER_FALLBACK``
defaults to ``none`` here, not cortex's ``lease``.

* **Fails open.** An unreachable broker (or a 5xx) is cortex's fallback: no gating, the
  configured endpoint. A 4xx (a bad token, an incompatible version), a bad config or any
  other error taking the lease is logged and the work goes to the configured endpoint too.
  Either way the grant's ``state`` says so, and the nightly run records it in ``extra``.
* **Refusals are not backend failures.** A refused or timed-out lease, or a grant whose
  node URL fails vetting, raises :class:`GpuLeaseUnavailable` (not a ``RuntimeError`` or a
  ``requests`` error). ``LLMClient`` then makes no model call for that scope, so the circuit
  breaker never counts it, and the files go in ``unreviewed_files`` (a blind scan).
* **Released on exit**, exceptions included: the lease is released with cortex's outcome
  (``ok``, ``failed``, ``cancelled``), also when resolving the node URL fails.
* **Node URLs are vetted** (:func:`vet_node_url`): a bare ``http(s)://host[:port]`` that is
  not a cloud metadata address. ``AI_AUDIT_OLLAMA_NODE_URLS`` (operator config,
  ``node=url,...``) wins over the broker's URL and is not vetted. A grant on
  ``ARGUS_GPU_HOME_NODE`` keeps the configured endpoint.
* **Footprint** comes from ``config/gpu_lease.yaml``: ``vram_mb`` is the whole footprint,
  ``model_mb`` the weights, which iris doesn't ask for again while the model is loaded.
  ``gpu-tenant.yaml``'s kinds carry the same figures.

Mirrors agent-composer's ``rag-chat/app/gpu_lease.py``, synchronous.
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import logging
import math
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "gpu_lease.yaml"
MANIFEST_PATH = ROOT / "gpu-tenant.yaml"
TENANT = "ai-code-auditor"
PRIORITY = "batch"  # the nightly run lands in iris's nightly-audit schedule window
KIND_REPO = "repo-audit"  # one lease per repo in the nightly pass
KIND_CALL = "review"  # one model call outside a repo scope (diff audits, equivalence, wiki ask)


class GpuLeaseUnavailable(Exception):
    """The broker refused the lease, didn't grant it in time, or granted an unusable node.

    Not an LLM-backend failure: never count it against the circuit breaker."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class LeaseGrant:
    """What the body of :func:`ollama_lease` runs with.

    ``state``: ``off`` (no coordinator), ``granted`` (by the broker), ``local`` (a local
    coordinator), ``fallback`` (broker unreachable, cortex's fallback) or ``unleased``
    (the broker answered 4xx, or the lease couldn't be taken; fails open)."""

    state: str
    endpoint: str
    node: str | None = None
    lease_id: str | None = None
    detail: str = ""
    handle: Any = None

    def report_progress(self, done: float, total: float | None = None, *, stage: str | None = None) -> None:
        """Progress for iris's dashboard, sent with the next heartbeat (a change also keeps the lease from
        reading as stalled). No-op without a lease."""
        if self.handle is not None:
            self.handle.report_progress(done, total, stage=stage)


# --- footprint -----------------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _footprints(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("GPU lease footprints not loaded from %s: %s", path, exc)
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _geometry(model: str) -> dict[str, Any] | None:
    """Exact model name, then the name without its tag."""
    models = _footprints().get("models") or {}
    geo = models.get(model)
    if geo is None:
        geo = models.get(model.rsplit(":", 1)[0])
    return geo if isinstance(geo, dict) else None


def model_mb(model: str) -> int | None:
    """The weights: layers x layer size, rounded up. ``None`` when the model isn't in the config or its geometry
    lacks a positive ``num_layers`` or ``layer_size_mb``, which the broker reads as "all of it is working set"."""
    geo = _geometry(model)
    if geo is None:
        return None
    try:
        layers, layer_mb = float(geo["num_layers"]), float(geo["layer_size_mb"])
    except (KeyError, TypeError, ValueError):
        return None  # incomplete or unreadable geometry: unknown, never "the weights cost nothing"
    if not (layers > 0 and layer_mb > 0) or math.isinf(layers * layer_mb):
        return None
    return math.ceil(layers * layer_mb)  # a fractional layer size is multiplied, then rounded up once


def vram_mb(model: str) -> int:
    """The whole footprint: weights plus the non-layer overhead (embeddings, KV cache base), else the default."""
    geo = _geometry(model)
    if geo is None:
        return int(_footprints().get("default_vram_mb", 6000))
    weights = model_mb(model)
    if weights is None:  # unknown weights are never 0 MB: take the default, as for an unknown model
        return max(int(_footprints().get("default_vram_mb", 6000)), int(geo.get("overhead_mb", 0)))
    return weights + int(geo.get("overhead_mb", 0))


# --- node URLs -----------------------------------------------------------------------------------------------------

# Cloud metadata endpoints outside the link-local ranges.
_METADATA_ADDRESSES = frozenset(ipaddress.ip_address(a) for a in (
    "fd00:ec2::254",    # AWS IMDS over IPv6 (Nitro)
    "100.100.100.200",  # Alibaba Cloud metadata
))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _metadata_host(host: str) -> bool:
    """Is ``host`` a cloud metadata address literal, in any form the socket layer accepts: decimal or hex IPv4, a
    trailing dot, IPv4 inside IPv6. That is any link-local address (169.254.169.254, fe80::/10) plus
    :data:`_METADATA_ADDRESSES`."""
    host = host.strip().rstrip(".").strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            return False
    inner = None
    if isinstance(ip, ipaddress.IPv6Address):
        packed = int(ip)
        inner = ip.ipv4_mapped or ip.sixtofour or (
            ipaddress.IPv4Address(packed & 0xFFFFFFFF)
            if ip in _NAT64 or (packed >> 32 == 0 and packed > 0xFFFF) else None)
    return any(a is not None and (a.is_link_local or a in _METADATA_ADDRESSES) for a in (ip, inner))


def vet_node_url(url: str) -> str:
    """A broker-supplied node URL as a bare origin, or :class:`GpuLeaseUnavailable`.

    Every Ollama call of the lease goes there, so it must be ``http(s)://host[:port]`` with no credentials, path,
    query or fragment, and its host must not be a cloud metadata address: a compromised or misconfigured broker
    can't point the auditor at metadata or smuggle a path into the call.
    """
    try:
        parts = urlsplit((url or "").strip())
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        parts = None
    if (parts is None or parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise GpuLeaseUnavailable(f"GPU broker gave an unusable node URL {url!r}: expected http(s)://host[:port]")
    if _metadata_host(parts.hostname):
        raise GpuLeaseUnavailable(f"GPU broker gave a cloud metadata node URL {url!r}, which is never allowed")
    return f"{parts.scheme}://{parts.netloc}"


def _node_urls() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in os.getenv("AI_AUDIT_OLLAMA_NODE_URLS", "").split(","):
        node, sep, url = item.strip().partition("=")
        if sep and node.strip() and url.strip():
            out[node.strip()] = url.strip().rstrip("/")
    return out


def _on_origin(origin: str, endpoint: str) -> str:
    """*endpoint*'s path and query on *origin* (``http://jarrah:11434`` + ``/api/generate``)."""
    ep = urlsplit(endpoint)
    o = urlsplit(origin)
    return urlunsplit((o.scheme, o.netloc, ep.path, ep.query, ""))


def endpoint_for(handle: Any, endpoint: str) -> str:
    """Where to send the lease's calls: *endpoint* (configured) unless the broker placed the work on another node.

    A node in ``AI_AUDIT_OLLAMA_NODE_URLS`` uses that origin; a grant on ``ARGUS_GPU_HOME_NODE`` (or naming no
    node) keeps *endpoint*; any other node's ``base_url`` must pass :func:`vet_node_url`. A grant on another node
    with no URL is refused: running it on the configured Ollama would load the model where nothing was reserved.
    """
    if getattr(handle, "via", "local") != "broker":
        return endpoint
    node = handle.node
    node_urls = _node_urls()
    if node and node in node_urls:
        return _on_origin(node_urls[node], endpoint)
    if not node or node == os.getenv("ARGUS_GPU_HOME_NODE", "").strip():
        return endpoint
    if not handle.base_url:
        raise GpuLeaseUnavailable(
            f"GPU broker placed the work on node {node!r} without a URL, and AI_AUDIT_OLLAMA_NODE_URLS has none for it")
    return _on_origin(vet_node_url(handle.base_url), endpoint)


# --- the lease -----------------------------------------------------------------------------------------------------

class _Off:
    """No coordinator: no lease is taken (``ARGUS_GPU_COORDINATOR`` unset, or one that couldn't be built)."""

    name = "none"


_OFF = _Off()
_coordinator: Any = None
_coordinator_error = ""


def _requested() -> str:
    return os.getenv("ARGUS_GPU_COORDINATOR", "none").strip().lower() or "none"


def coordinator() -> Any:
    """The process's coordinator, built once from the environment (``none`` when unset). A coordinator that can't
    be built is logged and replaced by ``none``: it must not stop the run."""
    global _coordinator, _coordinator_error
    if _coordinator is None:
        if _requested() == "none":
            _coordinator = _OFF
            return _coordinator
        timeout = os.getenv("ARGUS_GPU_LEASE_TIMEOUT_S", "").strip()
        manifest = os.getenv("ARGUS_GPU_TENANT_MANIFEST", "").strip() or (
            str(MANIFEST_PATH) if MANIFEST_PATH.exists() else None)
        try:
            from argus_cortex.gpu import build_coordinator

            _coordinator = build_coordinator(
                _requested(),
                timeout_s=float(timeout) if timeout else None,
                broker_url=os.getenv("ARGUS_GPU_BROKER_URL", "").strip() or None,
                broker_token=os.getenv("ARGUS_GPU_BROKER_TOKEN", "").strip() or None,
                tenant=os.getenv("ARGUS_GPU_TENANT", "").strip() or TENANT,
                manifest_path=manifest,
                fallback=os.getenv("ARGUS_GPU_BROKER_FALLBACK", "none"),
            )
        except Exception as exc:  # noqa: BLE001 - a bad GPU config must not stop the audit
            _coordinator_error = f"GPU coordinator {_requested()!r} not built: {exc}"
            logger.error("%s; calling Ollama unleased", _coordinator_error)
            _coordinator = _OFF
        if _coordinator.name != "none":
            logger.info("GPU leases for Ollama: %s coordinator", _coordinator.name)
    return _coordinator


@contextlib.contextmanager
def ollama_lease(model: str, *, kind: str, caller: str, endpoint: str,
                 priority: str = PRIORITY) -> Iterator[LeaseGrant]:
    """Hold an ``ollama:<model>`` lease for the body; yields a :class:`LeaseGrant` whose ``endpoint`` to call.

    Raises :class:`GpuLeaseUnavailable` on entry when the lease is refused, times out, or names an unusable node;
    every other failure to take it fails open (``state`` ``fallback`` or ``unleased``, the configured endpoint).
    """
    coord = coordinator()
    if coord.name == "none":
        if _requested() != "none":  # asked for a broker but there is none to ask: say so in the run's extra
            detail = _coordinator_error or f"ARGUS_GPU_COORDINATOR={_requested()} but no broker is configured"
            yield LeaseGrant(state="unleased", endpoint=endpoint, detail=detail)
        else:
            yield LeaseGrant(state="off", endpoint=endpoint)
        return

    from argus_cortex.gpu import GpuLeaseRefused, GpuLeaseTimeout

    target = f"ollama:{model}"
    try:
        cm = coord.lease(
            caller=caller[:200],
            min_vram_mb=vram_mb(model),
            model_mb=model_mb(model),
            priority=priority,
            target=target,
            kind=kind,
            preemptible=False,
        )
        handle = cm.__enter__()
    except GpuLeaseRefused as exc:
        raise GpuLeaseUnavailable(f"GPU broker refused {kind} on {target}: {exc}") from exc
    except GpuLeaseTimeout as exc:
        raise GpuLeaseUnavailable(f"no GPU lease for {kind} on {target} in time: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - BrokerError (a 4xx), a bad request: fail open, never fail the run
        detail = f"GPU broker error for {kind} on {target} ({type(exc).__name__}: {exc})"
        logger.error("%s; calling Ollama unleased", detail)
        yield LeaseGrant(state="unleased", endpoint=endpoint, detail=detail)
        return

    # Everything after the grant runs inside the try, so the lease is released even if resolving the node's URL
    # fails or the body raises.
    try:
        via = getattr(handle, "via", "local")
        try:
            url = endpoint_for(handle, endpoint)
        except GpuLeaseUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - an unresolvable grant is unusable, not a reason to run unleased
            raise GpuLeaseUnavailable(f"GPU lease {handle.lease_id} on node {handle.node!r} unusable: {exc}") from exc
        state = {"broker": "granted", "fallback": "fallback"}.get(via, "local")
        detail = "GPU broker unreachable; cortex fallback, no gating" if state == "fallback" else ""
        if state == "fallback":
            logger.warning("%s for %s on %s", detail, kind, target)
        elif url != endpoint:
            logger.info("lease %s: %s on %s placed on %s (%s)", handle.lease_id, kind, target, handle.node, url)
        yield LeaseGrant(state=state, endpoint=url, node=handle.node, lease_id=handle.lease_id,
                         detail=detail, handle=handle)
    except BaseException as exc:
        if not cm.__exit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        cm.__exit__(None, None, None)
