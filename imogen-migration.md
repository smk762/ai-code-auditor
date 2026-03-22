# Ecosystem Migration Instructions

Generated 2026-03-22. Covers two workstreams that are independent and can be
executed in parallel.  Step numbers within each workstream are sequential.

---

## Background

Three scheduling/UI concerns have grown beyond imogen's scope:

1. **GPU job scheduling** — imogen has an asset-affinity scheduler
   (`gallery/scheduler.py`) that optimises checkpoint/LoRA-aware dispatch across
   its own flux and sdxl backends.  `ai-code-auditor` has a separate
   `auditor/gpu_scheduler.py` that polls imogen/vidita queue depth and VRAM
   before starting LLM inference.  Neither knows about the other, so they can
   race for the same GPU (`192.168.1.109`).  A shared GPU job pool in gothmog
   solves this.

2. **Gallery UI** — imogen's gallery (image browser, compare, optimiser, monte
   carlo, param sweep, catalog, leaderboard, stats, runs, poses) is the right
   UX to share across image / video / text.  Somnus is the canonical operator
   frontend; the gallery should live there.

3. **"Fine tune" naming** — imogen's "fine tune" page is inference evaluation
   (fixed LoRA stack + checkpoint), not LoRA training (loraline's job).
   **Already renamed** to "Param Sweep" in this repo (nav label, page heading,
   button text).  URL `/fine-tune` and all API routes are unchanged.

---

## Workstream A: gothmog — GPU Job Pool

**Repo:** `gothmog` (currently `192.168.1.128`)
**Files to create:** `server/gpu_pool.py`, `server/gpu_pool_api.py`
**Files to modify:** `server/config.py`, `server/main.py`, `compose.yaml`

### A-1  Core scheduler — `server/gpu_pool.py`

Port `BackendScheduler` and `GlobalScheduler` verbatim from
`imogen/gallery/scheduler.py`, then extend as follows.

#### Changes from the imogen original

**Job registry** — the gothmog pool must allow callers to poll for results,
so every submitted job needs a durable record:

```python
from dataclasses import dataclass, field
from enum import Enum

class JobStatus(str, Enum):
    QUEUED     = "queued"
    DISPATCHED = "dispatched"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"

@dataclass
class JobRecord:
    job_id:        str
    backend:       str
    status:        JobStatus = JobStatus.QUEUED
    result:        dict | None = None
    error:         str | None = None
    submitted_at:  float = field(default_factory=time.time)
    dispatched_at: float | None = None
    completed_at:  float | None = None
```

Store in a module-level `_job_registry: dict[str, JobRecord] = {}` (in-memory
is fine; jobs are short-lived relative to the process lifetime).  Persist
`run_totals` / `run_dispatched` in Redis as the original does.

**Backend types** — two dispatch patterns:

```python
# In BackendScheduler.__init__, accept backend_type: str ("sync" | "async")
# and (for async) submit_path / jobs_path strings.

# "sync"  — POST {url}/generate, blocks until JSON response.
#           Used for: imogen_flux (:8001), imogen_sdxl (:8002).

# "async" — POST {submit_path} → 202 {id, status: "queued"},
#           then poll GET {jobs_path}/{id} until completed/failed/cancelled.
#           Used for: vidita_wan22 (gateway :8000 /videos/generate).
```

Replace the `call_generate` import with an internal `_dispatch_job` coroutine:

```python
async def _dispatch_job(
    backend_cfg: dict,
    payload: dict,
    timeout: int,
) -> dict:
    if backend_cfg["type"] == "sync":
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout))
        ) as client:
            resp = await client.post(
                f"{backend_cfg['url']}/generate", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    # async (vidita-style): submit → poll
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(
            f"{backend_cfg['url']}{backend_cfg['submit_path']}",
            json=payload,
        )
        resp.raise_for_status()
        job_id = resp.json()["id"]

    deadline = time.monotonic() + timeout
    poll_url = f"{backend_cfg['url']}{backend_cfg['jobs_path']}/{job_id}"
    while time.monotonic() < deadline:
        await asyncio.sleep(backend_cfg.get("poll_interval_s", 3.0))
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            r = await client.get(poll_url)
            r.raise_for_status()
            data = r.json()
        status = data.get("status", "")
        if status == "completed":
            return data
        if status in ("failed", "dead_letter", "cancelled"):
            raise RuntimeError(
                f"vidita job {job_id} {status}: "
                f"{data.get('error_message', 'unknown')}"
            )
    raise TimeoutError(f"vidita job {job_id} timed out after {timeout}s")
```

Update `BackendScheduler._run_once` to:
1. Create a `JobRecord` when a job is dequeued (status → DISPATCHED).
2. Call `_dispatch_job(backend_cfg, best.payload, best.timeout)`.
3. Set `JobRecord.status = COMPLETED / FAILED` and store `result` / `error`.
4. Resolve `best.future` as before (for the in-process SSE path).

**`GlobalScheduler.setup`** — build backends from config:

```python
BACKENDS = {
    "imogen_flux": {
        "type": "sync",
        "url": settings.gpu_pool_imogen_flux_url,
        "state_url": f"{settings.gpu_pool_imogen_flux_url}/state",
    },
    "imogen_sdxl": {
        "type": "sync",
        "url": settings.gpu_pool_imogen_sdxl_url,
        "state_url": f"{settings.gpu_pool_imogen_sdxl_url}/state",
    },
    "vidita_wan22": {
        "type": "async",
        "url": settings.gpu_pool_vidita_url,
        "submit_path": "/videos/generate",
        "jobs_path": "/videos/jobs",
        "poll_interval_s": 3.0,
        # vidita does not expose /state — use /health for queue_depth
        "health_url": f"{settings.gpu_pool_vidita_url}/health",
    },
}
```

For the vidita backend, `_refresh_state` should call `GET /health` and extract
`queue_depth` and `busy` from the health response (vidita gateway exposes
`queue_depth` on `/health`).  The scoring for vidita ignores checkpoint/LoRA
hits (set both to 0.0) — only wait bonus and lag penalty apply.

#### CapacityManager (ai-auditor support)

Add a `CapacityManager` class to the same file:

```python
import secrets
from datetime import datetime, timezone

@dataclass
class CapacityToken:
    token_id:     str
    caller:       str
    acquired_at:  datetime
    expires_at:   datetime
    vram_free_mb: int

class CapacityManager:
    """Issues VRAM capacity tokens for non-generation GPU consumers (ai-auditor).

    A token grants the holder permission to use the GPU for local LLM inference.
    The manager checks that all generation backends are idle and that the GPU
    has sufficient free VRAM before issuing a token.
    """

    def __init__(
        self,
        scheduler: GlobalScheduler,
        imogen_health_url: str,
        min_vram_mb: int = 6000,
        token_ttl_seconds: int = 7200,
    ) -> None:
        self._scheduler        = scheduler
        self._imogen_health_url = imogen_health_url
        self._min_vram_mb      = min_vram_mb
        self._token_ttl_s      = token_ttl_seconds
        self._tokens: dict[str, CapacityToken] = {}
        self._lock = asyncio.Lock()

    async def _get_vram_free_mb(self) -> int | None:
        """Read free VRAM from imogen's /health endpoint."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as c:
                r = await c.get(self._imogen_health_url)
                if r.status_code == 200:
                    data = r.json()
                    total = int(data.get("vram_total_mb", 0))
                    used  = int(data.get("vram_used_mb", 0))
                    if total > 0:
                        return total - used
        except Exception:
            pass
        return None

    async def acquire(
        self, caller: str, min_vram_mb: int, timeout_s: int
    ) -> CapacityToken:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # Check all generation backends are idle.
            all_idle = all(
                not sched._state.get("busy") and len(sched._pending) == 0
                for sched in self._scheduler._schedulers.values()
            )
            if all_idle:
                vram_free = await self._get_vram_free_mb()
                if vram_free is not None and vram_free >= min_vram_mb:
                    token = CapacityToken(
                        token_id    = secrets.token_hex(16),
                        caller      = caller,
                        acquired_at = datetime.now(timezone.utc),
                        expires_at  = datetime.now(timezone.utc)
                                      + timedelta(seconds=self._token_ttl_s),
                        vram_free_mb = vram_free,
                    )
                    async with self._lock:
                        self._tokens[token.token_id] = token
                    logger.info(
                        "gpu_capacity_acquired",
                        caller=caller,
                        token=token.token_id,
                        vram_free_mb=vram_free,
                    )
                    return token
            await asyncio.sleep(30.0)
        raise TimeoutError(
            f"GPU capacity not available after {timeout_s}s "
            f"(caller={caller}, min_vram_mb={min_vram_mb})"
        )

    async def release(self, token_id: str) -> bool:
        async with self._lock:
            token = self._tokens.pop(token_id, None)
        if token:
            logger.info("gpu_capacity_released", token=token_id,
                        caller=token.caller)
        return token is not None

    def expire_stale(self) -> None:
        """Evict tokens past their TTL (call periodically)."""
        now = datetime.now(timezone.utc)
        stale = [t for t in self._tokens.values() if t.expires_at < now]
        for t in stale:
            self._tokens.pop(t.token_id, None)
            logger.warning("gpu_capacity_token_expired", token=t.token_id,
                           caller=t.caller)
```

Instantiate `capacity_manager` as a module-level singleton alongside
`gpu_pool_scheduler`.

---

### A-2  HTTP router — `server/gpu_pool_api.py`

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from server.config import get_settings
from server.gpu_pool import (
    JobStatus,
    _job_registry,
    capacity_manager,
    gpu_pool_scheduler,
)
from server.main import _verify_key  # reuse existing Bearer auth

router = APIRouter(prefix="/v1/gpu", tags=["gpu_pool"])


# ── Job submission ────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    backend:    str
    payload:    dict
    run_id:     str | None = None
    run_total:  int = 1
    timeout:    int | None = None


@router.post("/submit", status_code=202)
async def submit_job(
    body: SubmitRequest,
    _key: str = Depends(_verify_key),
) -> dict:
    future = await gpu_pool_scheduler.enqueue(
        body.backend,
        body.payload,
        run_id=body.run_id,
        run_total=body.run_total,
        timeout=body.timeout,
    )
    # The job_id is assigned inside enqueue; find it by the future object.
    job_id = next(
        rec.job_id
        for rec in _job_registry.values()
        if rec._future is future  # BackendScheduler attaches future to record
    )
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    _key: str = Depends(_verify_key),
) -> dict:
    rec = _job_registry.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id":        rec.job_id,
        "backend":       rec.backend,
        "status":        rec.status,
        "result":        rec.result,
        "error":         rec.error,
        "submitted_at":  rec.submitted_at,
        "dispatched_at": rec.dispatched_at,
        "completed_at":  rec.completed_at,
    }


@router.delete("/runs/{run_id}")
async def cancel_run(
    run_id: str,
    _key: str = Depends(_verify_key),
) -> dict:
    n = await gpu_pool_scheduler.cancel_run(run_id)
    return {"cancelled": n}


# ── Backend state ─────────────────────────────────────────────────────────────

@router.get("/backends")
async def list_backends(_key: str = Depends(_verify_key)) -> list[dict]:
    out = []
    for name, sched in gpu_pool_scheduler._schedulers.items():
        state = sched._state
        out.append({
            "name":            name,
            "queue_depth":     len(sched._pending),
            "busy":            bool(state.get("busy")),
            "checkpoint":      state.get("checkpoint"),
            "lora_cache_count": len(state.get("lora_cache", [])),
        })
    return out


# ── Capacity tokens (for ai-auditor) ─────────────────────────────────────────

class AcquireRequest(BaseModel):
    caller:      str
    min_vram_mb: int = Field(default=6000, ge=1000)
    timeout_s:   int = Field(default=3600, ge=60)


@router.post("/capacity/acquire")
async def acquire_capacity(
    body: AcquireRequest,
    _key: str = Depends(_verify_key),
) -> dict:
    try:
        token = await capacity_manager.acquire(
            body.caller, body.min_vram_mb, body.timeout_s
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "token":       token.token_id,
        "acquired_at": token.acquired_at.isoformat(),
        "vram_free_mb": token.vram_free_mb,
    }


@router.delete("/capacity/tokens/{token_id}")
async def release_capacity(
    token_id: str,
    _key: str = Depends(_verify_key),
) -> dict:
    released = await capacity_manager.release(token_id)
    if not released:
        raise HTTPException(status_code=404, detail="Token not found or already released")
    return {"released": True}
```

Note: the `submit_job` endpoint needs a slight restructure in `gpu_pool.py` so
that `enqueue` returns both the `asyncio.Future` and the `job_id` (or the
`ScheduledJob` object).  The cleanest approach is to have `enqueue` return the
`ScheduledJob` directly and let callers choose whether to await `job.future` or
just record the `job_id`.

---

### A-3  Config additions — `server/config.py`

Add to the `Settings` class:

```python
# ── GPU Job Pool ──────────────────────────────────────────────────────────────
gpu_pool_imogen_flux_url:  str   = "http://192.168.1.109:8001"
gpu_pool_imogen_sdxl_url:  str   = "http://192.168.1.109:8002"
gpu_pool_vidita_url:       str   = "http://192.168.1.109:8000"
gpu_pool_imogen_health_url: str  = "http://192.168.1.109:8003/health"
gpu_pool_redis_url:         str  = ""   # falls back to redis_url if empty

# Scheduler scoring weights (same semantics as imogen's gallery/config.py)
gpu_pool_checkpoint_hit_weight:    float = 50.0
gpu_pool_lora_hit_weight:          float = 10.0
gpu_pool_wait_weight:              float = 0.1
gpu_pool_run_lag_penalty:          float = 5.0
gpu_pool_checkpoint_drain_first:   bool  = True
gpu_pool_checkpoint_explicit_first: bool = True
gpu_pool_busy_poll_max_seconds:    int   = 30

# Capacity manager
gpu_pool_capacity_vram_min_mb:       int = 6000
gpu_pool_capacity_token_ttl_seconds: int = 7200
```

---

### A-4  Lifespan wiring — `server/main.py`

In the `lifespan` async context manager, add after existing startup:

```python
from server.gpu_pool import capacity_manager, gpu_pool_scheduler
from server.gpu_pool_api import router as gpu_pool_router

# startup
gpu_pool_scheduler.setup(get_settings())
gpu_pool_scheduler.start()

# shutdown (before existing close_db)
gpu_pool_scheduler.stop()
```

And register the router:

```python
app.include_router(gpu_pool_router)
```

The router uses `_verify_key` from `server/main.py`.  Move `_verify_key` into
`server/auth.py` (or just import it in `gpu_pool_api.py` without circular
issues by importing from `server.main` after the app is created — check that
this doesn't cause a circular import; if it does, move the dependency to a
shared `server/auth.py`).

---

### A-5  Compose env vars — `compose.yaml`

Add to the `gothmog` service environment block:

```yaml
- GPU_POOL_IMOGEN_FLUX_URL=${GPU_POOL_IMOGEN_FLUX_URL:-http://192.168.1.109:8001}
- GPU_POOL_IMOGEN_SDXL_URL=${GPU_POOL_IMOGEN_SDXL_URL:-http://192.168.1.109:8002}
- GPU_POOL_VIDITA_URL=${GPU_POOL_VIDITA_URL:-http://192.168.1.109:8000}
- GPU_POOL_IMOGEN_HEALTH_URL=${GPU_POOL_IMOGEN_HEALTH_URL:-http://192.168.1.109:8003/health}
- GPU_POOL_REDIS_URL=${GPU_POOL_REDIS_URL:-}
- GPU_POOL_CAPACITY_VRAM_MIN_MB=${GPU_POOL_CAPACITY_VRAM_MIN_MB:-6000}
```

---

### A-6  Tests — `tests/test_gpu_pool.py`

Write unit tests (no live HTTP) covering:

- `BackendScheduler._score` — checkpoint hit adds weight, LoRA hits scale with
  count, wait bonus increases with elapsed time, lag penalty kicks in when one
  run is ahead.
- `BackendScheduler._job_matches_loaded_checkpoint` — explicit match, no
  checkpoint in payload (always matches), mismatch.
- `GlobalScheduler.enqueue` / `cancel_run` — mock `BackendScheduler`.
- `CapacityManager.acquire` — mock `_get_vram_free_mb` and scheduler state; test
  immediate grant, deferred grant after idle, timeout path.
- `CapacityManager.expire_stale` — tokens past TTL are evicted.
- API routes — use FastAPI `TestClient` with mocked pool singletons.

---

### A-7  Downstream step (imogen — do after gothmog A ships)

Once gothmog's GPU pool API is live, replace `imogen/gallery/scheduler.py` with
a thin client (`gallery/gpu_pool_client.py`):

```python
"""Thin client that submits generation jobs to gothmog's GPU pool API."""
import asyncio, httpx, structlog, time

logger = structlog.get_logger()

class GothmogPoolClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def enqueue(self, backend, payload, *, run_id=None,
                      run_total=1, timeout=None) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        asyncio.create_task(self._submit_and_poll(
            backend, payload, run_id, run_total, timeout, fut
        ))
        return fut

    async def submit(self, backend, payload, *, run_id=None,
                     run_total=1, timeout=None) -> dict:
        fut = await self.enqueue(backend, payload, run_id=run_id,
                                 run_total=run_total, timeout=timeout)
        return await fut

    async def _submit_and_poll(self, backend, payload, run_id,
                                run_total, timeout, fut):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    f"{self._base}/v1/gpu/submit",
                    json={"backend": backend, "payload": payload,
                          "run_id": run_id, "run_total": run_total,
                          "timeout": timeout},
                    headers=self._headers,
                )
                r.raise_for_status()
                job_id = r.json()["job_id"]
            result = await self._poll(job_id, timeout or 300)
            if not fut.done():
                fut.set_result(result)
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)

    async def _poll(self, job_id: str, timeout: int) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"{self._base}/v1/gpu/jobs/{job_id}",
                    headers=self._headers,
                )
                r.raise_for_status()
                data = r.json()
            if data["status"] == "completed":
                return data["result"]
            if data["status"] in ("failed", "cancelled"):
                raise RuntimeError(
                    f"GPU pool job {job_id} {data['status']}: "
                    f"{data.get('error', 'unknown')}"
                )
        raise TimeoutError(f"GPU pool job {job_id} timed out after {timeout}s")

    async def cancel_run(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.delete(
                f"{self._base}/v1/gpu/runs/{run_id}",
                headers=self._headers,
            )

# Module-level singleton — replaces gallery/scheduler.py's `scheduler` object
scheduler = GothmogPoolClient(
    base_url=settings.gothmog_url,
    api_key=settings.gothmog_api_key,
)
```

Update `gallery/config.py` to add `gothmog_url` and `gothmog_api_key`.
Update `gallery/generate.py`'s `scheduler_generate` and `scheduler_enqueue`
to call `scheduler.submit` / `scheduler.enqueue` from the new client.
The `gallery/plan.py` `stream_plan` function is unchanged — it calls
`scheduler_enqueue` from `gallery.generate`.

### A-8  Downstream step (ai-code-auditor — do after gothmog A ships)

Replace `auditor/gpu_scheduler.py`'s `GpuScheduler.wait_for_capacity()` body
with a call to gothmog's capacity API:

```python
async def wait_for_capacity_gothmog(
    gothmog_url: str,
    api_key: str,
    min_vram_mb: int = 6000,
    timeout_s: int = 3600,
) -> dict:
    """Call gothmog GPU pool capacity API.  Blocks until a token is issued."""
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(float(timeout_s + 30))) as c:
        r = await c.post(
            f"{gothmog_url.rstrip('/')}/v1/gpu/capacity/acquire",
            json={"caller": "ai-auditor", "min_vram_mb": min_vram_mb,
                  "timeout_s": timeout_s},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        return r.json()  # {"token", "acquired_at", "vram_free_mb"}
```

Keep `release` as a best-effort call at pipeline end.
Add `GOTHMOG_URL` and `GOTHMOG_API_KEY` to `config/gpu_scheduler.yaml` and
the auditor config loader.

---

## Workstream B: somnus — Gallery UI

**Repo:** `somnus` (currently `192.168.1.128`)
**Prereq:** None — can be built while gothmog workstream A is in progress.
**Imogen gallery stays fully active throughout — somnus is additive.**

### B-1  Env var

Add to `.env.local.example`:

```
IMOGEN_API_URL=http://192.168.1.109:8003
```

### B-2  API proxy — `src/app/api/imogen/[...path]/route.ts`

Create a catch-all Next.js route handler that proxies all requests to imogen's
gallery API, preserving headers, body, query params, and streaming responses:

```typescript
import { NextRequest, NextResponse } from "next/server";

const IMOGEN_URL = process.env.IMOGEN_API_URL ?? "http://192.168.1.109:8003";

async function handler(req: NextRequest, { params }: { params: { path: string[] } }) {
  const path = params.path.join("/");
  const search = req.nextUrl.search;
  const target = `${IMOGEN_URL}/${path}${search}`;

  const upstreamResp = await fetch(target, {
    method: req.method,
    headers: {
      "content-type": req.headers.get("content-type") ?? "application/json",
    },
    body: req.method !== "GET" && req.method !== "HEAD"
      ? req.body
      : undefined,
    // @ts-expect-error — Node 18 fetch duplex
    duplex: "half",
  });

  // Pass SSE streams through unchanged.
  const contentType = upstreamResp.headers.get("content-type") ?? "";
  if (contentType.includes("text/event-stream")) {
    return new NextResponse(upstreamResp.body, {
      status: upstreamResp.status,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
      },
    });
  }

  const body = await upstreamResp.arrayBuffer();
  return new NextResponse(body, {
    status: upstreamResp.status,
    headers: { "content-type": contentType },
  });
}

export const GET = handler;
export const POST = handler;
export const DELETE = handler;
export const PATCH = handler;
```

This single route handles all gallery API calls.  Media objects (`/media/**`)
are also proxied through this route — no direct CDN/MinIO access from the
browser.

### B-3  Typed client — `src/lib/imogen-api.ts`

Add a typed client for imogen gallery endpoints.  Minimum surface needed for
all gallery pages:

```typescript
const BASE = "/api/imogen";

// Images
export const getImages = (params: ImageQuery) =>
  fetch(`${BASE}/api/images?${qs(params)}`).then(r => r.json());

export const deleteImage = (id: string) =>
  fetch(`${BASE}/api/images/${id}`, { method: "DELETE" }).then(r => r.json());

export const rateImage = (id: string, rating: number) =>
  fetch(`${BASE}/api/images/${id}/rate`, {
    method: "POST", body: JSON.stringify({ rating }),
    headers: { "content-type": "application/json" },
  }).then(r => r.json());

// Stats / Leaderboard
export const getStats = () =>
  fetch(`${BASE}/api/stats`).then(r => r.json());

export const getLeaderboard = (params?: LeaderboardQuery) =>
  fetch(`${BASE}/api/leaderboard?${qs(params ?? {})}`).then(r => r.json());

// Catalog
export const getCatalog = () =>
  fetch(`${BASE}/api/catalog`).then(r => r.json());

export const getCatalogEntry = (id: string) =>
  fetch(`${BASE}/api/catalog/${id}`).then(r => r.json());

// Runs
export const getRuns = (params?: RunQuery) =>
  fetch(`${BASE}/api/runs?${qs(params ?? {})}`).then(r => r.json());

export const getRun = (id: string) =>
  fetch(`${BASE}/api/runs/${id}`).then(r => r.json());

export const cancelRun = (id: string) =>
  fetch(`${BASE}/api/runs/${id}/cancel`, { method: "POST" }).then(r => r.json());

// Optimiser
export const analysePrompt = (body: AnalyseRequest) =>
  fetch(`${BASE}/api/optimiser/analyse`, {
    method: "POST", body: JSON.stringify(body),
    headers: { "content-type": "application/json" },
  }).then(r => r.json());

// Poses
export const getPoses = () =>
  fetch(`${BASE}/api/poses`).then(r => r.json());

// SSE streams (compare / monte-carlo / optimiser / param-sweep)
export const streamRun = (endpoint: string, body: unknown) =>
  fetch(`${BASE}${endpoint}`, {
    method: "POST", body: JSON.stringify(body),
    headers: { "content-type": "application/json" },
  }); // caller reads .body as ReadableStream
```

### B-4  Gallery route pages

Add all routes under `src/app/gallery/`.  Each page is a Next.js Server or
Client Component — use Client Components where real-time SSE or interactive
state is needed.  Below is the full route map and the key UI elements for each.

| Route | Type | Key UI elements |
|-------|------|-----------------|
| `gallery/page.tsx` | Client | Masonry/grid image browser; filter bar (backend, rating, NSFW, date); infinite scroll or pagination; click opens detail modal |
| `gallery/[imageId]/page.tsx` | Client | Full image, generation params table, rating controls, action buttons (delete, regenerate, optimise) |
| `gallery/compare/page.tsx` | Client | Prompt inputs, backend checkboxes, LoRA/checkpoint selectors, SSE progress stream, result grid |
| `gallery/monte-carlo/page.tsx` | Client | Prompt inputs, LoRA weight range sliders, iteration count, SSE progress, result grid with weight annotations |
| `gallery/optimiser/page.tsx` | Client | Prompt input, analysis panel, per-backend prompt translation, SSE generation progress, result grid |
| `gallery/param-sweep/page.tsx` | Client | Prompt + variable substitution table, fixed LoRA stack table (model + weight per checkpoint), SSE progress |
| `gallery/catalog/page.tsx` | Client | Cards for each catalog entry (model name, type, preview images) |
| `gallery/catalog/[catalogId]/page.tsx` | Client | Entry detail: description, sample images, notes |
| `gallery/leaderboard/page.tsx` | Client | Ranked table of LoRA/checkpoint combos by average rating; filter by backend |
| `gallery/stats/page.tsx` | Client | Charts: generation count over time, rating distribution, backend utilisation |
| `gallery/runs/page.tsx` | Client | Table of all generation runs with status, type, created_at; click expands output thumbnails |
| `gallery/poses/page.tsx` | Client | Pose preset grid; click copies pose_key; optional reference image → pose converter |

#### SSE helper hook

The SSE-based pages (compare, monte-carlo, optimiser, param-sweep) should share
a custom hook:

```typescript
// src/hooks/use-generation-stream.ts
import { useState, useCallback } from "react";

export type StreamEvent =
  | { type: "progress"; message: string; pct: number }
  | { type: "result"; imageUrl: string; meta: Record<string, unknown> }
  | { type: "error"; message: string }
  | { type: "done" };

export function useGenerationStream(endpoint: string) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [running, setRunning] = useState(false);

  const start = useCallback(async (body: unknown) => {
    setEvents([]);
    setRunning(true);
    try {
      const resp = await fetch(`/api/imogen${endpoint}`, {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "content-type": "application/json" },
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const evt = JSON.parse(line.slice(6));
            setEvents(prev => [...prev, evt]);
          } catch { /* skip malformed */ }
        }
      }
    } finally {
      setRunning(false);
    }
  }, [endpoint]);

  return { events, running, start };
}
```

### B-5  Nav integration

In the existing nav component (likely `src/components/nav.tsx` or
`src/app/layout.tsx`), add a "Gallery" entry pointing to `/gallery`.

### B-6  Retire imogen gallery (separate step — NOT this task)

Once the somnus gallery is confirmed working end-to-end, open a follow-up task
on imogen to:
- Remove `gallery/templates/`, `gallery/static/js/`, `gallery/routes/pages.py`
- Keep all `gallery/routes/api.py`, `gallery/routes/*_api.py` endpoints (somnus
  proxy still calls them)
- Remove gallery-only Python dependencies if any

---

## Summary of affected files per repo

| Repo | Host | Access | Files |
|------|------|--------|-------|
| `gothmog` | `.128` | remote | `server/gpu_pool.py` (new), `server/gpu_pool_api.py` (new), `server/config.py`, `server/main.py`, `compose.yaml`, `tests/test_gpu_pool.py` (new) |
| `somnus` | `.128` | remote | `src/app/gallery/**` (new), `src/app/api/imogen/[...path]/route.ts` (new), `src/lib/imogen-api.ts` (new), `src/hooks/use-generation-stream.ts` (new), nav component |
| `imogen` | `.109` | local | `gallery/gpu_pool_client.py` (new, after gothmog A), `gallery/scheduler.py` (remove, after gothmog A), `gallery/config.py`, `gallery/generate.py` |
| `ai-code-auditor` | `.109` | local | `auditor/gpu_scheduler.py` (replace polling with gothmog client, after gothmog A) |

Already done in imogen (this commit):
- `gallery/templates/base.html` — nav label "Fine tune" → "Param Sweep"
- `gallery/templates/fine_tune.html` — page title and heading updated
- `gallery/templates/poses.html` — cross-reference updated
- `.gitignore` — add `controlnet-*/`
