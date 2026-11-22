"""argus-iris GPU leases around local-Ollama work (auditor/gpu_lease.py, #51). No network: the broker is a fake
coordinator or cortex's real BrokerCoordinator over an httpx MockTransport."""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from argus_cortex.gpu import GpuLease, GpuLeaseRefused, GpuLeaseTimeout, NullCoordinator
from argus_cortex.gpu_broker import BrokerCoordinator, BrokerError
from argus_cortex.gpu_wire import TenantManifest

import auditor.llm_client as llm_mod
import auditor.repo_auditor as repo_auditor
from auditor import gpu_lease
from auditor.config import ModelProviderConfig
from auditor.contracts import CodeUnit
from auditor.gpu_lease import GpuLeaseUnavailable, ollama_lease
from auditor.llm_client import LLMClient

MODEL = "qwen2.5-coder:32b"
LOCAL = "http://ollama:11434/api/generate"


class FakeCoordinator:
    """Records each lease request; grants *handle* or raises *error* on entry, and records how each lease ended."""

    name = "broker"

    def __init__(self, handle: GpuLease | None = None, error: BaseException | None = None) -> None:
        self.handle = handle or GpuLease(via="broker", lease_id="L1", node="dragon", base_url="http://dragon:11434")
        self.error = error
        self.requests: list[dict[str, Any]] = []
        self.exits: list[str | None] = []

    @contextlib.contextmanager
    def lease(self, **kw: Any):
        self.requests.append(kw)
        if self.error:
            raise self.error
        try:
            yield self.handle
        except BaseException as exc:
            self.exits.append(type(exc).__name__)
            raise
        else:
            self.exits.append(None)


def _use(monkeypatch: pytest.MonkeyPatch, coord: Any) -> Any:
    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "broker")
    monkeypatch.setattr(gpu_lease, "_coordinator", coord)
    return coord


def _lease(**kw: Any):
    return ollama_lease(MODEL, kind=kw.pop("kind", "repo-audit"), caller="r1", endpoint=LOCAL, **kw)


def _client(provider: str = "ollama", endpoint: str = LOCAL) -> LLMClient:
    return LLMClient(provider_config=ModelProviderConfig(provider=provider, model=MODEL, endpoint=endpoint))


class _Resp:
    status_code = 200

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


def _record_posts(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any] | None = None) -> list[str]:
    urls: list[str] = []

    def post(url: str, **kw: Any) -> _Resp:
        urls.append(url)
        return _Resp(body or {"response": "[]", "choices": [{"message": {"content": "[]"}}]})

    monkeypatch.setattr(llm_mod.requests, "post", post)
    return urls


def _unit(path: str = "/m/r1/a.py") -> CodeUnit:
    return CodeUnit(id=path, repo="r1", file_path=path, symbol="f", kind="function", language="python",
                    start_line=1, end_line=2, raw_text="def f():\n    return 1\n")


# --- off by default, and never for a cloud provider ---------------------------------------------------------------

def test_off_by_default_takes_no_lease_and_calls_the_configured_endpoint(monkeypatch) -> None:
    with _lease() as grant:
        assert (grant.state, grant.endpoint, grant.handle) == ("off", LOCAL, None)
    assert gpu_lease.coordinator().name == "none"  # cortex's broker client is never built

    urls = _record_posts(monkeypatch)
    c = _client()
    assert c.generate("hi") == "[]"
    assert urls == [LOCAL] and c.lease_events == []


def test_a_cloud_provider_never_leases(monkeypatch) -> None:
    coord = _use(monkeypatch, FakeCoordinator())
    urls = _record_posts(monkeypatch)
    c = _client("openai-compatible", endpoint="https://api.example/v1/chat/completions")
    with c.gpu_lease("r1"):
        assert c.generate("hi") == "[]"
    assert c.generate("hi") == "[]"
    assert coord.requests == [] and c.lease_events == []
    assert urls == ["https://api.example/v1/chat/completions"] * 2


def test_broker_requested_without_a_url_is_unleased_and_says_so(monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "broker")
    monkeypatch.setenv("ARGUS_GPU_BROKER_FALLBACK", "none")
    with _lease() as grant:
        assert (grant.state, grant.endpoint) == ("unleased", LOCAL)
        assert "no broker" in grant.detail


# --- the lease request and the grant's base_url -------------------------------------------------------------------

def test_the_lease_asks_for_the_model_with_its_footprint(monkeypatch) -> None:
    coord = _use(monkeypatch, FakeCoordinator())
    with _lease() as grant:
        assert grant.state == "granted"
    req = coord.requests[0]
    assert (req["target"], req["kind"], req["priority"], req["preemptible"]) == (
        f"ollama:{MODEL}", "repo-audit", "batch", False)
    # From config/gpu_lease.yaml's geometry: 64 layers x 310 MiB weights, plus 2500 MiB overhead.
    assert (req["min_vram_mb"], req["model_mb"]) == (64 * 310 + 2500, 64 * 310)
    assert coord.exits == [None]


def test_footprint_lookup(monkeypatch) -> None:
    monkeypatch.setattr(gpu_lease, "_footprints", lambda: {"default_vram_mb": 6000, "models": {
        "qwen3": {"num_layers": 10, "layer_size_mb": 100, "overhead_mb": 50}}})
    assert (gpu_lease.vram_mb("qwen3:8b"), gpu_lease.model_mb("qwen3:8b")) == (1050, 1000)  # by name, tag dropped
    assert gpu_lease.vram_mb("llama3.2:3b") == 6000 and gpu_lease.model_mb("llama3.2:3b") is None


def test_ollama_calls_go_to_the_grants_base_url(monkeypatch) -> None:
    _use(monkeypatch, FakeCoordinator(GpuLease(via="broker", lease_id="L2", node="jarrah",
                                               base_url="http://jarrah:11434/")))
    urls = _record_posts(monkeypatch)
    c = _client()
    with c.gpu_lease("r1"):
        c.generate("a")
        c.generate("b")
    c.generate("c")  # outside the scope: a lease of its own, on the same grant here
    assert urls == ["http://jarrah:11434/api/generate"] * 3
    assert c.endpoint == LOCAL  # restored after each lease
    assert [e["state"] for e in c.lease_events] == ["granted", "granted"]
    assert c.lease_events[0] == {"caller": "r1", "kind": "repo-audit", "state": "granted", "node": "jarrah"}


def test_home_node_and_operator_node_urls(monkeypatch) -> None:
    _use(monkeypatch, FakeCoordinator(GpuLease(via="broker", lease_id="L3", node="dragon",
                                               base_url="http://dragon:11434")))
    monkeypatch.setenv("ARGUS_GPU_HOME_NODE", "dragon")
    with _lease() as grant:
        assert grant.endpoint == LOCAL
    monkeypatch.delenv("ARGUS_GPU_HOME_NODE")
    monkeypatch.setenv("AI_AUDIT_OLLAMA_NODE_URLS", "dragon=http://192.168.200.20:11434")
    with _lease() as grant:
        assert grant.endpoint == "http://192.168.200.20:11434/api/generate"


@pytest.mark.parametrize("bad", [
    "http://169.254.169.254", "http://169.254.169.254.:80", "http://2852039166", "http://0xA9FEA9FE",
    "http://[fe80::1]:11434", "http://[::ffff:169.254.169.254]",
    "http://[fd00:ec2::254]", "http://[FD00:EC2:0::254]/", "http://100.100.100.200", "http://1684301000",
    "http://[::ffff:100.100.100.200]",
    "http://jarrah:11434/admin", "http://u:p@jarrah:11434", "http://jarrah:11434?x=1", "file:///etc/passwd",
    "http://jarrah:99999",
])
def test_unsafe_node_urls_are_refused_and_the_lease_released(monkeypatch, bad: str) -> None:
    coord = _use(monkeypatch, FakeCoordinator(GpuLease(via="broker", lease_id="L4", node="jarrah", base_url=bad)))
    with pytest.raises(GpuLeaseUnavailable):
        with _lease():
            pytest.fail("the body must not run against an unvetted node URL")
    assert coord.exits == ["GpuLeaseUnavailable"]


def test_lan_and_other_unique_local_nodes_stay_usable(monkeypatch) -> None:
    for ok in ("http://[fd00:ec2::253]:11434", "http://100.100.100.201:11434", "http://192.168.200.109:11434"):
        _use(monkeypatch, FakeCoordinator(GpuLease(via="broker", lease_id="L5", node="jarrah", base_url=ok)))
        with _lease() as grant:
            assert grant.endpoint == ok + "/api/generate"


def test_another_node_without_a_url_is_refused_not_run_locally(monkeypatch) -> None:
    coord = _use(monkeypatch, FakeCoordinator(GpuLease(via="broker", lease_id="L6", node="jarrah", base_url=None)))
    with pytest.raises(GpuLeaseUnavailable, match="without a URL"):
        with _lease():
            pytest.fail("the body must not run on the configured Ollama for a lease on another node")
    assert coord.exits == ["GpuLeaseUnavailable"]


# --- release ------------------------------------------------------------------------------------------------------

def test_body_errors_propagate_and_release_the_lease(monkeypatch) -> None:
    coord = _use(monkeypatch, FakeCoordinator())
    with pytest.raises(ValueError):
        with _lease():
            raise ValueError("boom")
    assert coord.exits == ["ValueError"]

    c = _client()
    with pytest.raises(KeyError):
        with c.gpu_lease("r1"):
            monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: _Resp({"response": "[]"}))
            c.generate("a")
            raise KeyError("after a model call")
    assert coord.exits == ["ValueError", "KeyError"]
    assert c.endpoint == LOCAL and c._grant is None


def test_a_failure_resolving_the_url_releases_the_lease(monkeypatch) -> None:
    coord = _use(monkeypatch, FakeCoordinator())

    def broken(handle, endpoint):
        raise KeyError(handle.node)

    monkeypatch.setattr(gpu_lease, "endpoint_for", broken)
    with pytest.raises(GpuLeaseUnavailable):
        with _lease():
            pytest.fail("body must not run")
    assert coord.exits == ["GpuLeaseUnavailable"]


def test_a_scope_takes_one_lease_at_its_first_model_call(monkeypatch) -> None:
    coord = _use(monkeypatch, FakeCoordinator())
    _record_posts(monkeypatch)
    c = _client()
    with c.gpu_lease("cached-repo"):
        pass  # nothing to ask the model (all cached): no lease, no wait
    assert coord.requests == []
    with c.gpu_lease("r1"):
        with c.gpu_lease("nested"):
            c.generate("a")
        c.generate("b")
    assert len(coord.requests) == 1 and coord.exits == [None]


# --- fails open ---------------------------------------------------------------------------------------------------

def _broker(handler) -> BrokerCoordinator:
    return BrokerCoordinator("http://broker.test:8120", tenant="ai-code-auditor", token="t",
                             fallback=NullCoordinator(), transport=httpx.MockTransport(handler))


def test_unreachable_broker_fails_open_to_the_configured_endpoint(monkeypatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _use(monkeypatch, _broker(down))
    urls = _record_posts(monkeypatch)
    c = _client()
    with c.gpu_lease("r1"):
        assert c.generate("hi") == "[]"
    assert urls == [LOCAL]
    assert c.lease_events[0]["state"] == "fallback" and "unreachable" in c.lease_events[0]["detail"]


def test_broker_4xx_fails_open_and_is_logged(monkeypatch, caplog) -> None:
    _use(monkeypatch, _broker(lambda request: httpx.Response(401, json={"detail": "bad token"})))
    urls = _record_posts(monkeypatch)
    c = _client()
    with caplog.at_level(logging.ERROR, logger="auditor.gpu_lease"):
        with c.gpu_lease("r1"):
            assert c.generate("hi") == "[]"
    assert urls == [LOCAL]
    assert c.lease_events[0]["state"] == "unleased" and "BrokerError" in c.lease_events[0]["detail"]
    assert any("unleased" in r.getMessage() for r in caplog.records)


def test_any_error_taking_the_lease_fails_open(monkeypatch) -> None:
    _use(monkeypatch, FakeCoordinator(error=BrokerError("broker refused the request (400)")))
    with _lease() as grant:
        assert (grant.state, grant.endpoint) == ("unleased", LOCAL)
    monkeypatch.setattr(gpu_lease, "_coordinator", None)
    monkeypatch.setenv("ARGUS_GPU_BROKER_URL", "http://broker.test:8120")
    monkeypatch.setenv("ARGUS_GPU_TENANT", "Not A Slug!")  # BrokerCoordinator refuses it at build
    with _lease() as grant:
        assert (grant.state, grant.endpoint) == ("unleased", LOCAL)
        assert "not built" in grant.detail


def test_against_cortex_broker_client_grant_and_release(monkeypatch) -> None:
    """The real BrokerCoordinator, with a mock broker that grants on jarrah; the release reports the outcome."""
    seen: list[tuple[str, str, dict]] = []

    def broker(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((request.method, request.url.path, body))
        if request.url.path == "/leases":
            return httpx.Response(200, json={"lease_version": "1.1", "lease_id": "abc", "state": "granted",
                                             "node": "jarrah", "base_url": "http://jarrah:11434", "ttl_s": 60})
        if request.url.path == "/leases/abc/release":
            return httpx.Response(200, json={"lease_version": "1.1", "lease_id": "abc", "state": "released"})
        return httpx.Response(404)

    _use(monkeypatch, _broker(broker))
    with _lease() as grant:
        assert grant.endpoint == "http://jarrah:11434/api/generate"
    with pytest.raises(RuntimeError):
        with _lease():
            raise RuntimeError("the review crashed")
    method, path, req = seen[0]
    assert (method, path) == ("POST", "/leases")
    assert (req["target"], req["kind"], req["priority"], req["tenant"]) == (
        f"ollama:{MODEL}", "repo-audit", "batch", "ai-code-auditor")
    assert (req["vram_mb"], req["model_mb"]) == (gpu_lease.vram_mb(MODEL), gpu_lease.model_mb(MODEL))
    releases = [b["outcome"] for m, p, b in seen if p == "/leases/abc/release"]
    assert releases == ["ok", "failed"]


# --- refusals: not backend failures -------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [GpuLeaseRefused("can never fit"), GpuLeaseTimeout("queued 3 ahead")])
def test_refusal_and_timeout_are_lease_errors(monkeypatch, error) -> None:
    _use(monkeypatch, FakeCoordinator(error=error))
    with pytest.raises(GpuLeaseUnavailable) as caught:
        with _lease():
            pytest.fail("body must not run")
    assert not isinstance(caught.value, RuntimeError)


@pytest.mark.parametrize("error", [GpuLeaseRefused("can never fit"), GpuLeaseTimeout("queued 3 ahead")])
def test_a_refused_lease_skips_the_model_without_tripping_the_breaker(monkeypatch, error) -> None:
    monkeypatch.setenv("AI_AUDIT_LLM_BREAKER_FAILURES", "1")
    _use(monkeypatch, FakeCoordinator(error=error))
    urls = _record_posts(monkeypatch)
    c = _client()
    with c.gpu_lease("r1"):
        for i in range(3):
            c.analyze_code(_unit(f"/m/r1/f{i}.py"))
    assert c.generate("outside a scope") == ""
    assert urls == []  # no model call without a lease
    assert c.breaker.consecutive == 0 and not c.breaker.is_open and c._failures == 0
    # The scan can't vouch for these files: pack GC and the trust metrics must not read silence as a fix.
    assert c.unreviewed_files == {f"/m/r1/f{i}.py" for i in range(3)}
    assert [e["state"] for e in c.lease_events] == ["unavailable", "unavailable"]
    # The next scope asks again; the block ends with its scope.
    _use(monkeypatch, FakeCoordinator())
    with c.gpu_lease("r2"):
        assert c.generate("hi") == "[]"
    assert urls == ["http://dragon:11434/api/generate"]


# --- the repo pass ------------------------------------------------------------------------------------------------

def test_repo_audit_holds_one_lease_reports_progress_and_records_it(monkeypatch, tmp_path: Path) -> None:
    handle = GpuLease(via="broker", lease_id="L7", node="jarrah", base_url="http://jarrah:11434")
    coord = _use(monkeypatch, FakeCoordinator(handle))
    urls = _record_posts(monkeypatch)
    monkeypatch.setattr(repo_auditor, "resolve_scan_root", lambda repo: tmp_path)
    monkeypatch.setattr(repo_auditor, "build_repo_briefing", lambda *a, **k: "")
    monkeypatch.setattr(repo_auditor, "scan_repo", lambda repo: ["/m/r1/a.py", "/m/r1/b.py"])
    monkeypatch.setattr(repo_auditor, "extract_code_units", lambda repo, fp: [_unit(fp)])
    monkeypatch.setattr(repo_auditor, "run_static_checks", lambda *a: [])
    monkeypatch.setattr(llm_mod, "build_file_briefing", lambda **kw: "")
    from types import SimpleNamespace

    events: list[dict] = []
    repo_auditor.run_repo_audit(SimpleNamespace(name="r1"), llm=_client(), lease_events=events)
    assert len(coord.requests) == 1 and coord.exits == [None]
    assert urls == ["http://jarrah:11434/api/generate"] * 2
    assert (handle.progress, handle.stage) == (1.0, "r1: 2/2 units")
    assert events == [{"caller": "r1", "kind": "repo-audit", "state": "granted", "node": "jarrah"}]


# --- gpu-tenant.yaml ----------------------------------------------------------------------------------------------

def test_tenant_manifest_validates_and_matches_the_code() -> None:
    raw = yaml.safe_load(gpu_lease.MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = TenantManifest.model_validate(raw)
    assert manifest.tenant == gpu_lease.TENANT
    kinds = {k.kind: k for k in manifest.kinds}
    assert set(kinds) == {gpu_lease.KIND_REPO, gpu_lease.KIND_CALL}
    for k in kinds.values():
        model = k.target.removeprefix("ollama:")
        assert (k.vram_mb, k.model_mb, k.priority) == (gpu_lease.vram_mb(model), gpu_lease.model_mb(model),
                                                       gpu_lease.PRIORITY)
    generator = yaml.safe_load(Path("config/generator.yaml").read_text(encoding="utf-8"))
    assert kinds[gpu_lease.KIND_REPO].target == f"ollama:{generator['providers']['ollama']['model']}"
