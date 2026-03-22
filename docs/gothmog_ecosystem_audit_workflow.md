# Gothmog: `ecosystem_audit` Workflow

Implementation instructions for adding overnight audit scheduling to gothmog.
The ai-code-auditor exposes a lightweight HTTP API (`audit_api.py`) that follows
gothmog's existing submit → poll contract.  Gothmog handles VRAM gating,
service-state awareness, retry orchestration, and observability.

---

## Architecture

```
cron (host, nightly)
  │
  POST /orchestrate/run  {workflow: "ecosystem_audit"}
  │
  ▼
Gothmog worker
  │
  ├─ check_capacity_node
  │    GET imogen :8003/health        → vram_free_mb
  │    GET imogen :8003/state         → busy
  │    GET vidita :8000/health        → queue_depth   (gateway)
  │    GET audit  :8765/audit/health  → vram_free_mb (nvidia-smi)
  │
  │    if capacity insufficient:
  │      raise WorkflowStepError(retryable=True)   ← run marked failed/retryable
  │      cron retries in 30 min
  │
  │    if imogen idle + VRAM marginal:
  │      POST imogen :8003/unload     → recover VRAM
  │
  ├─ trigger_audit_node
  │    POST audit :8765/audit/run     → {run_id}
  │
  ├─ poll_audit_node
  │    GET  audit :8765/audit/runs/{run_id}
  │    deadline = 8 hours (covers largest overnight scan)
  │
  ├─ ingest_rag_node
  │    POST audit :8765/audit/ingest  → pushes briefings to mimiri/audit_docs
  │
  └─ report_node
       output: {run_id, repos_scanned, findings, violations, duration_ms}
```

---

## 1. Cron trigger (host-side, no changes to gothmog)

```cron
# /etc/cron.d/gothmog-audit
# Fire every 30 minutes between 01:00 and 06:00.
# Gothmog's capacity check exits immediately if GPU is busy;
# the next cron slot retries automatically.
*/30 1-6 * * * smk \
  curl -sf -X POST http://192.168.1.128:8030/orchestrate/run \
    -H "Authorization: Bearer $GOTHMOG_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"workflow":"ecosystem_audit","input":{}}' \
  >> /var/log/gothmog-audit-trigger.log 2>&1
```

Gothmog returns `202 {run_id}` if it accepts the job.
If capacity is unavailable the run completes quickly with `status: failed, retryable: true`.
The next 30-min slot re-submits; no sleeping workers, no queued backlog.

---

## 2. Config additions (`server/config.py`)

Add to the `Settings` class:

```python
# ── Ecosystem Audit ────────────────────────────────────────────────────────
audit_api_url: str = "http://192.168.1.109:8765"
audit_api_key: str = ""                          # optional; set if audit_api uses auth
audit_job_timeout_s: int = 28800                 # 8 hours max for a full overnight run
audit_job_poll_interval_s: float = 30.0          # poll every 30s (long-running job)
audit_vram_min_mb: int = 6000                    # minimum free VRAM to proceed
audit_imogen_url: str = "http://192.168.1.109:8003"
audit_vidita_gateway_url: str = "http://192.168.1.109:8000"
```

`.env.example` additions:

```dotenv
# ── Ecosystem Audit Workflow ─────────────────────────────────────────────────
AUDIT_API_URL=http://192.168.1.109:8765
AUDIT_API_KEY=
AUDIT_JOB_TIMEOUT_S=28800
AUDIT_JOB_POLL_INTERVAL_S=30
AUDIT_VRAM_MIN_MB=6000
AUDIT_IMOGEN_URL=http://192.168.1.109:8003
AUDIT_VIDITA_GATEWAY_URL=http://192.168.1.109:8000
```

---

## 3. Workflow code (`server/graphs.py`)

Add the following.  Follows all existing gothmog conventions:
`_invoke_with_retry`, `WorkflowStepError`, `httpx` for HTTP, TypedDict state.

### State

```python
class EcosystemAuditState(TypedDict, total=False):
    # ── config (passed in via input) ──────────────────────────────────────
    audit_api_url: str
    audit_api_key: str

    # ── capacity check results ────────────────────────────────────────────
    vram_free_mb: int
    imogen_busy: bool
    vidita_queue_depth: int

    # ── audit job tracking ────────────────────────────────────────────────
    audit_run_id: str

    # ── final output ──────────────────────────────────────────────────────
    output: dict[str, Any]
```

### Helpers

```python
def _audit_headers(api_key: str) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h
```

### Nodes

```python
def _check_capacity_node(state: EcosystemAuditState) -> dict:
    """Gate the run on GPU availability and downstream service state.

    Raises WorkflowStepError(retryable=True) if conditions are not met so
    that the cron trigger can retry in the next slot without blocking a worker.
    """
    s = get_settings()
    imogen_url  = state.get("audit_imogen_url",        s.audit_imogen_url).rstrip("/")
    vidita_url  = state.get("audit_vidita_gateway_url", s.audit_vidita_gateway_url).rstrip("/")
    audit_url   = state.get("audit_api_url",            s.audit_api_url).rstrip("/")
    vram_min    = int(state.get("audit_vram_min_mb",    s.audit_vram_min_mb))

    reasons: list[str] = []

    # ── 1. Audit service VRAM (nvidia-smi via audit health endpoint) ──────
    try:
        r = httpx.get(f"{audit_url}/audit/health", timeout=10)
        r.raise_for_status()
        body = r.json()
        vram_free = int(body.get("vram_free_mb", 0))
    except Exception as exc:
        raise WorkflowStepError(
            step="check_capacity",
            code="AUDIT_HEALTH_UNREACHABLE",
            message=f"Audit service health check failed: {exc}",
            retryable=True,
        )

    # ── 2. Imogen state (GPU lock + loaded VRAM) ──────────────────────────
    imogen_busy     = False
    imogen_vram     = 0
    try:
        health = httpx.get(f"{imogen_url}/health", timeout=5).json()
        state_r = httpx.get(f"{imogen_url}/state",  timeout=5).json()
        imogen_busy  = bool(state_r.get("busy", False))
        imogen_vram  = int(health.get("vram_used_mb", 0))
    except Exception:
        pass  # imogen unreachable → treat as idle, proceed

    # ── 3. Vidita queue depth ─────────────────────────────────────────────
    vidita_queue = 0
    try:
        vh = httpx.get(f"{vidita_url}/health", timeout=5).json()
        vidita_queue = int(vh.get("queue_depth", 0))
    except Exception:
        pass  # vidita unreachable → treat as idle

    # ── 4. Gate checks ────────────────────────────────────────────────────
    if imogen_busy:
        reasons.append(f"imogen inference lock held")
    if vidita_queue > 0:
        reasons.append(f"vidita has {vidita_queue} job(s) queued")
    if vram_free < vram_min:
        reasons.append(f"only {vram_free} MB VRAM free (need {vram_min} MB)")

    if reasons:
        raise WorkflowStepError(
            step="check_capacity",
            code="CAPACITY_UNAVAILABLE",
            message="GPU not available: " + "; ".join(reasons),
            retryable=True,
        )

    # ── 5. Opportunistic imogen unload if VRAM is marginal ────────────────
    # "Marginal" = enough to pass the gate but not enough for full GPU inference.
    # Unloading recovers the VRAM imogen holds so the audit model runs faster.
    FULL_GPU_THRESHOLD_MB = 20000
    if vram_free < FULL_GPU_THRESHOLD_MB and imogen_vram > 0 and not imogen_busy:
        try:
            httpx.post(f"{imogen_url}/unload", timeout=10)
            import time as _time; _time.sleep(3)  # let torch.cuda.empty_cache() settle
        except Exception:
            pass  # unload failure is non-fatal; audit continues with partial offload

    return {
        "vram_free_mb":        vram_free,
        "imogen_busy":         imogen_busy,
        "vidita_queue_depth":  vidita_queue,
    }


def _trigger_audit_node(state: EcosystemAuditState) -> dict:
    """Submit a new audit run to the ai-code-auditor API."""
    s = get_settings()
    url     = state.get("audit_api_url", s.audit_api_url).rstrip("/")
    api_key = state.get("audit_api_key", s.audit_api_key)

    resp = _invoke_with_retry(
        workflow="ecosystem_audit",
        step="trigger_audit",
        retries=2,
        fn=lambda: httpx.post(
            f"{url}/audit/run",
            json={},
            headers=_audit_headers(api_key),
            timeout=30,
        ).raise_for_status() or httpx.post(
            f"{url}/audit/run",
            json={},
            headers=_audit_headers(api_key),
            timeout=30,
        ).json(),
    )
    run_id = str(resp.get("run_id", ""))
    if not run_id:
        raise WorkflowStepError(
            step="trigger_audit",
            code="NO_RUN_ID",
            message="Audit API returned no run_id",
            retryable=False,
        )
    return {"audit_run_id": run_id}


def _poll_audit_node(state: EcosystemAuditState) -> dict:
    """Poll audit run until complete or deadline exceeded."""
    s        = get_settings()
    url      = state.get("audit_api_url", s.audit_api_url).rstrip("/")
    api_key  = state.get("audit_api_key", s.audit_api_key)
    run_id   = state["audit_run_id"]
    timeout  = int(state.get("audit_job_timeout_s", s.audit_job_timeout_s))
    interval = float(state.get("audit_job_poll_interval_s", s.audit_job_poll_interval_s))

    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        r = httpx.get(
            f"{url}/audit/runs/{run_id}",
            headers=_audit_headers(api_key),
            timeout=15,
        )
        r.raise_for_status()
        body   = r.json()
        status = str(body.get("status", "")).lower()

        if status == "completed":
            return {"output": body}
        if status in {"failed", "cancelled"}:
            raise WorkflowStepError(
                step="poll_audit",
                code="AUDIT_RUN_FAILED",
                message=body.get("error", f"Audit run {run_id} failed"),
                retryable=False,
            )
        time.sleep(interval)

    raise WorkflowStepError(
        step="poll_audit",
        code="AUDIT_TIMEOUT",
        message=f"Audit run {run_id} did not complete within {timeout}s",
        retryable=True,
    )


def _ingest_rag_node(state: EcosystemAuditState) -> dict:
    """After a successful audit, push fresh briefings to the audit RAG collection.

    Non-fatal: if mimiri is unreachable the audit results are still persisted locally.
    """
    s       = get_settings()
    url     = state.get("audit_api_url", s.audit_api_url).rstrip("/")
    api_key = state.get("audit_api_key", s.audit_api_key)
    try:
        httpx.post(
            f"{url}/audit/ingest",
            headers=_audit_headers(api_key),
            timeout=60,
        ).raise_for_status()
    except Exception as exc:
        # Log but do not fail the workflow — audit data is persisted regardless.
        import logging
        logging.getLogger(__name__).warning("RAG ingest failed (non-fatal): %s", exc)
    return {}


def _report_audit_node(state: EcosystemAuditState) -> dict:
    out = state.get("output", {})
    return {
        "output": {
            "run_id":         state.get("audit_run_id"),
            "repos_scanned":  out.get("scanned_repos", []),
            "files_scanned":  out.get("scanned_files", 0),
            "findings":       out.get("findings", 0),
            "violations":     out.get("violations", 0),
            "errors":         out.get("errors", []),
            "duration_ms":    out.get("duration_ms"),
            "vram_free_mb":   state.get("vram_free_mb"),
        }
    }
```

### Graph

```python
ECOSYSTEM_AUDIT_WORKFLOW_ID = "ecosystem_audit"

def build_ecosystem_audit_graph() -> StateGraph:
    graph = StateGraph(EcosystemAuditState)
    graph.add_node("check_capacity", _check_capacity_node)
    graph.add_node("trigger_audit",  _trigger_audit_node)
    graph.add_node("poll_audit",     _poll_audit_node)
    graph.add_node("ingest_rag",     _ingest_rag_node)
    graph.add_node("report",         _report_audit_node)

    graph.set_entry_point("check_capacity")
    graph.add_edge("check_capacity", "trigger_audit")
    graph.add_edge("trigger_audit",  "poll_audit")
    graph.add_edge("poll_audit",     "ingest_rag")
    graph.add_edge("ingest_rag",     "report")
    graph.add_edge("report",         END)
    return graph.compile()
```

### WORKFLOW_REGISTRY entry

```python
ECOSYSTEM_AUDIT_WORKFLOW_ID: {
    "name":        "Ecosystem Audit",
    "description": "Overnight code audit across all ecosystem repos. "
                   "Gates on GPU availability; raises retryable error if busy "
                   "so cron-based retry fires every 30 min until capacity is free.",
    "graph":       build_ecosystem_audit_graph,
    "state_class": EcosystemAuditState,
    "timeout_s":   30000,   # 8h audit + 30m buffer
},
```

---

## 4. Audit API endpoints (ai-code-auditor side)

The auditor exposes three endpoints so gothmog can follow its standard
submit → poll pattern.  See `auditor/audit_api.py` in this repo.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/audit/health` | VRAM info + service readiness (used by `check_capacity_node`) |
| `POST` | `/audit/run`    | Start a new audit run; returns `{run_id}` immediately |
| `GET`  | `/audit/runs/{run_id}` | Poll run status and results |
| `POST` | `/audit/ingest` | Trigger RAG briefing ingest to mimiri after a completed run |

Start alongside the pipeline:

```bash
# From ai-code-auditor root
.venv/bin/uvicorn auditor.audit_api:app --host 0.0.0.0 --port 8765
```

Or as a background service — add to the same systemd unit or Docker container as the pipeline.

---

## 5. Retry behaviour summary

| Scenario | WorkflowStepError code | retryable | Cron action |
|---|---|---|---|
| VRAM too low | `CAPACITY_UNAVAILABLE` | ✅ | Fires again in 30 min |
| imogen inference lock | `CAPACITY_UNAVAILABLE` | ✅ | Fires again in 30 min |
| vidita jobs queued | `CAPACITY_UNAVAILABLE` | ✅ | Fires again in 30 min |
| Audit API unreachable | `AUDIT_HEALTH_UNREACHABLE` | ✅ | Fires again in 30 min |
| Audit run itself failed | `AUDIT_RUN_FAILED` | ❌ | Investigate `pipeline.log` |
| Audit run timed out (>8h) | `AUDIT_TIMEOUT` | ✅ | Fires next night |

---

## 6. Observability

Once the workflow is registered, gothmog's existing Prometheus metrics
(`workflow_runs_total`, `workflow_duration_seconds`, `workflow_step_retries_total`)
cover the audit workflow automatically.  In Sauron (Grafana), filter by
`workflow="ecosystem_audit"` to see nightly run history, duration trends,
and capacity-wait retry counts.
