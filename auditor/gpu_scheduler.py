"""GPU scheduling gate for the audit pipeline.

Before a run starts, ``GpuScheduler.wait_for_capacity()`` will:

1. Read free VRAM from ``nvidia-smi``.
2. Poll configured downstream services (imogen, vidita) for queue depth / busy state.
3. If imogen's queue is empty and not busy, optionally POST /unload to recover its VRAM.
4. Compute how many model layers fit on GPU; return a ``GpuCapacity`` describing the
   offload configuration to use.
5. If conditions are not met, sleep ``retry_interval_s`` and try again indefinitely.

No pip dependencies beyond the stdlib + ``requests`` (already present).
VRAM is read via ``nvidia-smi`` so no ``pynvml`` install is required.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "gpu_scheduler.yaml"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class VramInfo:
    free_mb: int
    total_mb: int

    @property
    def used_mb(self) -> int:
        return self.total_mb - self.free_mb


@dataclass
class ServiceStatus:
    name: str
    reachable: bool = False
    queue_depth: int = 0
    busy: bool = False
    vram_used_mb: int = 0
    detail: str = ""


@dataclass
class GpuCapacity:
    """Offload configuration resolved by the scheduler."""
    vram_free_mb: int
    # Number of transformer layers to place on GPU.
    # -1  → let Ollama decide (all layers fit)
    #  0  → CPU-only (not recommended; very slow)
    #  N  → N layers on GPU, remainder on CPU RAM
    num_gpu_layers: int
    # Human-readable label for logging / metadata
    offload_mode: str  # "full" | "partial" | "cpu_heavy"
    services: list[ServiceStatus] = field(default_factory=list)


# ---------------------------------------------------------------------------
# VRAM
# ---------------------------------------------------------------------------

def get_vram_info() -> VramInfo | None:
    """Query ``nvidia-smi`` for GPU 0 free / total VRAM.  Returns None on failure."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning("nvidia-smi failed: %s", result.stderr.strip())
            return None
        line = result.stdout.strip().splitlines()[0]
        free_mb, total_mb = (int(x.strip()) for x in line.split(","))
        return VramInfo(free_mb=free_mb, total_mb=total_mb)
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not read VRAM info: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Downstream service polling
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 5) -> dict[str, Any] | None:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.debug("GET %s failed: %s", url, exc)
        return None


def _post(url: str, timeout: int = 10) -> bool:
    try:
        resp = requests.post(url, timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.debug("POST %s failed: %s", url, exc)
        return False


def poll_imogen(cfg: dict[str, Any]) -> ServiceStatus:
    status = ServiceStatus(name="imogen")
    health = _get(cfg["health_url"])
    if health is None:
        status.detail = "unreachable"
        return status
    status.reachable = True
    status.vram_used_mb = int(health.get("vram_used_mb", 0))

    state = _get(cfg["state_url"])
    if state:
        status.busy = bool(state.get("busy", False))

    # imogen does not expose queue_depth on the model server directly;
    # treat busy-lock as an in-flight job.
    status.queue_depth = 1 if status.busy else 0
    return status


def poll_vidita(cfg: dict[str, Any]) -> ServiceStatus:
    status = ServiceStatus(name="vidita")
    health = _get(cfg["health_url"])
    if health is None:
        status.detail = "unreachable"
        return status
    status.reachable = True
    status.queue_depth = int(health.get("queue_depth", 0))
    status.busy = status.queue_depth > 0
    return status


def request_imogen_unload(cfg: dict[str, Any]) -> bool:
    logger.info("Requesting imogen model unload to recover VRAM …")
    ok = _post(cfg["unload_url"])
    if ok:
        logger.info("imogen unload accepted.")
    else:
        logger.warning("imogen unload request failed.")
    return ok


# ---------------------------------------------------------------------------
# Layer / offload calculation
# ---------------------------------------------------------------------------

def compute_num_gpu_layers(vram_free_mb: int, layer_cfg: dict[str, Any]) -> tuple[int, str]:
    """Return ``(num_gpu_layers, mode)`` given available VRAM.

    ``num_gpu_layers == -1`` means "all layers on GPU" (pass -1 or omit from request).
    """
    num_layers: int = int(layer_cfg.get("num_layers", 64))
    layer_size_mb: int = int(layer_cfg.get("layer_size_mb", 310))
    overhead_mb: int = int(layer_cfg.get("overhead_mb", 2500))

    available = max(0, vram_free_mb - overhead_mb)
    layers_that_fit = available // layer_size_mb

    if layers_that_fit >= num_layers:
        return -1, "full"
    if layers_that_fit >= num_layers // 2:
        return layers_that_fit, "partial"
    return layers_that_fit, "cpu_heavy"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _load_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        logger.warning("gpu_scheduler.yaml not found at %s, using defaults", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class GpuScheduler:
    def __init__(self, config_path: Path = _CONFIG_PATH):
        self._cfg = _load_config(config_path)
        self.retry_interval_s: int = int(self._cfg.get("retry_interval_s", 1800))
        self.vram_min_mb: int = int(self._cfg.get("vram_min_mb", 6000))
        self.vram_full_gpu_mb: int = int(self._cfg.get("vram_full_gpu_mb", 20000))
        self.layer_cfg: dict[str, Any] = self._cfg.get("model_layers", {})
        self.services_cfg: dict[str, Any] = self._cfg.get("services", {})
        self.gothmog_url: str = self._cfg.get("gothmog_url", "").strip()
        self.gothmog_api_key: str = self._cfg.get("gothmog_api_key", "").strip()
        self._capacity_token_id: str | None = None

    # ------------------------------------------------------------------
    def wait_for_capacity(self) -> GpuCapacity:
        """Block until GPU capacity is available, then return the offload config.

        If ``gothmog_url`` is configured, delegates to gothmog's
        ``POST /v1/gpu/capacity/acquire`` and blocks there.
        Otherwise falls back to direct nvidia-smi / service polling.

        Set env var ``AI_AUDIT_SKIP_GPU_SCHEDULER=1`` to bypass all checks
        (useful in CI or on CPU-only machines).
        """
        import os
        if os.getenv("AI_AUDIT_SKIP_GPU_SCHEDULER", "").strip() in {"1", "true", "yes"}:
            logger.info("GPU scheduler bypassed via AI_AUDIT_SKIP_GPU_SCHEDULER.")
            return GpuCapacity(vram_free_mb=0, num_gpu_layers=0, offload_mode="bypassed")

        if self.gothmog_url:
            return self._acquire_via_gothmog()

        # Direct-poll fallback (no gothmog).
        attempt = 0
        while True:
            attempt += 1
            capacity = self._check_once(attempt)
            if capacity is not None:
                return capacity
            logger.info(
                "Conditions not met (attempt %d). Retrying in %d min …",
                attempt,
                self.retry_interval_s // 60,
            )
            time.sleep(self.retry_interval_s)

    # ------------------------------------------------------------------
    def _acquire_via_gothmog(self) -> GpuCapacity:
        """POST to gothmog /v1/gpu/capacity/acquire, retrying on 503.

        Gothmog returns 503 when capacity is not yet available.  We sleep
        ``retry_interval_s`` between attempts so the audit pipeline never
        races with imogen/vidita for the GPU.
        """
        url = self.gothmog_url.rstrip("/") + "/v1/gpu/capacity/acquire"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.gothmog_api_key:
            headers["Authorization"] = f"Bearer {self.gothmog_api_key}"
        payload = {
            "caller": "ai-auditor",
            "min_vram_mb": self.vram_min_mb,
            "timeout_s": self.retry_interval_s,
        }

        attempt = 0
        while True:
            attempt += 1
            logger.info(
                "Requesting GPU capacity from gothmog at %s (attempt %d, min_vram_mb=%d) …",
                self.gothmog_url,
                attempt,
                self.vram_min_mb,
            )
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=self.retry_interval_s + 30)
                if resp.status_code == 503:
                    logger.info(
                        "gothmog: capacity unavailable (503). Retrying in %d min …",
                        self.retry_interval_s // 60,
                    )
                    time.sleep(self.retry_interval_s)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.HTTPError:
                raise
            except Exception as exc:
                logger.error("gothmog capacity acquire failed: %s", exc)
                raise

            self._capacity_token_id = data.get("token")
            vram_free = int(data.get("vram_free_mb", 0))
            logger.info(
                "GPU capacity granted by gothmog — token=%s vram_free_mb=%d",
                self._capacity_token_id,
                vram_free,
            )
            return GpuCapacity(
                vram_free_mb=vram_free,
                num_gpu_layers=-1,   # gothmog asserts GPU is idle; run all layers on GPU
                offload_mode="gothmog_managed",
            )

    def release_capacity(self) -> None:
        """Release the gothmog capacity token (best-effort; call in finally)."""
        if not self._capacity_token_id or not self.gothmog_url:
            return
        url = (
            self.gothmog_url.rstrip("/")
            + f"/v1/gpu/capacity/tokens/{self._capacity_token_id}"
        )
        headers: dict[str, str] = {}
        if self.gothmog_api_key:
            headers["Authorization"] = f"Bearer {self.gothmog_api_key}"
        try:
            requests.delete(url, headers=headers, timeout=10)
            logger.info("GPU capacity token %s released.", self._capacity_token_id)
        except Exception as exc:
            logger.warning("Failed to release GPU capacity token: %s", exc)
        finally:
            self._capacity_token_id = None

    # ------------------------------------------------------------------
    def _check_once(self, attempt: int) -> GpuCapacity | None:
        # 1. Read VRAM ──────────────────────────────────────────────────
        vram = get_vram_info()
        if vram is None:
            # No nvidia-smi / no GPU — proceed in CPU-only mode rather than looping forever.
            logger.warning("No GPU detected — proceeding with CPU-only offload mode.")
            return GpuCapacity(vram_free_mb=0, num_gpu_layers=0, offload_mode="cpu_only")
        logger.info(
            "Attempt %d — VRAM: %d MB free / %d MB total",
            attempt, vram.free_mb, vram.total_mb,
        )

        # 2. Poll services ──────────────────────────────────────────────
        statuses: list[ServiceStatus] = []
        imogen_cfg = self.services_cfg.get("imogen", {})
        vidita_cfg = self.services_cfg.get("vidita", {})

        if imogen_cfg.get("enabled", False):
            s = poll_imogen(imogen_cfg)
            statuses.append(s)
            logger.info(
                "imogen — reachable=%s queue=%d busy=%s vram_used=%d MB",
                s.reachable, s.queue_depth, s.busy, s.vram_used_mb,
            )

        if vidita_cfg.get("enabled", False):
            s = poll_vidita(vidita_cfg)
            statuses.append(s)
            logger.info(
                "vidita  — reachable=%s queue=%d",
                s.reachable, s.queue_depth,
            )

        # 3. Check downstream queues ────────────────────────────────────
        for s in statuses:
            svc_cfg = self.services_cfg.get(s.name, {})
            max_q = int(svc_cfg.get("max_queue_depth", 0))
            if s.reachable and s.queue_depth > max_q:
                logger.info(
                    "%s has %d job(s) in queue (max allowed: %d) — deferring.",
                    s.name, s.queue_depth, max_q,
                )
                return None

        # 4. Try to recover VRAM from imogen if we're short ─────────────
        if vram.free_mb < self.vram_min_mb:
            imogen_s = next((s for s in statuses if s.name == "imogen"), None)
            if (
                imogen_s
                and imogen_s.reachable
                and not imogen_s.busy
                and imogen_s.vram_used_mb > 0
            ):
                request_imogen_unload(imogen_cfg)
                time.sleep(3)  # allow torch.cuda.empty_cache() to settle
                vram = get_vram_info() or vram
                logger.info("VRAM after imogen unload: %d MB free", vram.free_mb)

        # 5. Final VRAM gate ────────────────────────────────────────────
        if vram.free_mb < self.vram_min_mb:
            logger.info(
                "VRAM still insufficient: %d MB free (need %d MB minimum).",
                vram.free_mb, self.vram_min_mb,
            )
            return None

        # 6. Compute offload ────────────────────────────────────────────
        num_gpu, mode = compute_num_gpu_layers(vram.free_mb, self.layer_cfg)
        logger.info(
            "GPU capacity OK — mode=%s num_gpu_layers=%s (%d MB free)",
            mode, num_gpu if num_gpu >= 0 else "all", vram.free_mb,
        )
        return GpuCapacity(
            vram_free_mb=vram.free_mb,
            num_gpu_layers=num_gpu,
            offload_mode=mode,
            services=statuses,
        )
