"""Lightweight merge-to-dev refresh: sync one mirror → diff audit → packs + RAG.

Nightly / ecosystem runs remain the source of truth for full-suite world models.
This path keeps agent pack + ``audit_docs`` RAG fresh after merges without
re-running the whole ecosystem.

Debounce
--------
* **Per-repo** — rapid pushes to the same repo reset a quiet window
  (``AI_AUDIT_MERGE_DEBOUNCE_S``, default 900s / 15 min) and coalesce to the
  earliest ``before`` + latest ``after`` SHAs.
* **Global gap** — after one merge job finishes, wait
  ``AI_AUDIT_MERGE_GLOBAL_GAP_S`` (default 120s) before starting the next
  pending repo so burst merges across the suite do not stampede the LLM.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_ZERO_SHA = "0" * 40


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def merge_hook_enabled() -> bool:
    return _bool_env("AI_AUDIT_MERGE_HOOK_ENABLED", True)


def merge_debounce_s() -> int:
    return max(0, _int_env("AI_AUDIT_MERGE_DEBOUNCE_S", 900))


def merge_global_gap_s() -> int:
    return max(0, _int_env("AI_AUDIT_MERGE_GLOBAL_GAP_S", 120))


def merge_branch() -> str:
    return (os.getenv("AI_AUDIT_MERGE_BRANCH") or "dev").strip() or "dev"


def merge_webhook_secret() -> str:
    return (os.getenv("AI_AUDIT_MERGE_WEBHOOK_SECRET") or "").strip()


@dataclass
class MergeEvent:
    repo: str
    before: str = ""
    after: str = ""
    ref: str = ""
    source: str = "api"  # api | github | cli
    received_at: float = field(default_factory=time.time)
    force: bool = False  # skip debounce when True
    sync_mirror: bool = True

    def coalesce(self, other: "MergeEvent") -> "MergeEvent":
        """Keep earliest non-zero ``before`` and latest ``after`` across a burst."""
        older, newer = (
            (self, other) if self.received_at <= other.received_at else (other, self)
        )
        before = ""
        for ev in (older, newer):
            if ev.before and ev.before != _ZERO_SHA:
                before = ev.before
                break
        after = newer.after or older.after
        return MergeEvent(
            repo=self.repo or other.repo,
            before=before,
            after=after,
            ref=newer.ref or older.ref,
            source=newer.source or older.source,
            received_at=older.received_at,
            force=self.force or other.force,
            sync_mirror=self.sync_mirror and other.sync_mirror,
        )


@dataclass
class MergeJobStatus:
    repo: str
    state: str  # pending | running | done | error | skipped
    before: str = ""
    after: str = ""
    scheduled_for: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_github_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Validate ``X-Hub-Signature-256`` when a webhook secret is configured."""
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature_header.strip())


def parse_github_push_event(payload: dict[str, Any]) -> MergeEvent | None:
    """Return a MergeEvent for pushes to the configured branch, else None."""
    ref = str(payload.get("ref") or "")
    branch = merge_branch()
    expected = f"refs/heads/{branch}"
    if ref != expected:
        return None
    repo_obj = payload.get("repository") or {}
    name = str(repo_obj.get("name") or "").strip()
    if not name:
        return None
    return MergeEvent(
        repo=name,
        before=str(payload.get("before") or ""),
        after=str(payload.get("after") or ""),
        ref=ref,
        source="github",
    )


def _git_rev(repo_path, rev: str = "HEAD") -> str:
    import subprocess
    from pathlib import Path

    r = subprocess.run(
        ["git", "-C", str(Path(repo_path)), "rev-parse", rev],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def run_merge_refresh(
    event: MergeEvent,
    *,
    output_dir: str | None = None,
    sync_mirror: bool = True,
    ingest_rag: bool | None = None,
) -> dict[str, Any]:
    """Sync one repo mirror, diff-audit the merge range, emit packs (+ optional RAG)."""
    from auditor.agent_pack import (
        emit_packs_from_findings,
        emit_world_model_packs,
        pack_rag_ingest_enabled,
    )
    from auditor.config import load_ecosystem_config
    from auditor.diff_auditor import get_diff_between_refs, run_diff_audit
    from auditor.repo_resolver import resolve_repo_path
    from conductor.mirrors import sync_one_mirror
    from conductor.publish import public_base_url

    cfg = load_ecosystem_config()
    out = output_dir or cfg.output_dir
    do_rag = pack_rag_ingest_enabled(ingest_rag)
    repo_cfg = next((r for r in cfg.repos if r.name == event.repo), None)
    if repo_cfg is None:
        # Allow bare GitHub repo name matching ecosystem entry case-insensitively.
        repo_cfg = next(
            (r for r in cfg.repos if r.name.lower() == event.repo.lower()),
            None,
        )
    if repo_cfg is None:
        raise ValueError(f"Repo {event.repo!r} not found in ecosystem config")

    run_id = f"merge-{repo_cfg.name}-{int(time.time())}"
    sync_info: dict[str, Any] = {"skipped": not sync_mirror}
    before = event.before
    after = event.after

    if sync_mirror:
        # Capture tip before fetch when webhook did not supply SHAs.
        try:
            path_pre = resolve_repo_path(repo_cfg)
            if path_pre.exists() and not before:
                before = _git_rev(path_pre, "HEAD")
        except Exception:
            pass
        sync_result = sync_one_mirror(repo_cfg.name, branch=merge_branch())
        sync_info = {
            "ok": sync_result.ok,
            "action": sync_result.action,
            "branch": sync_result.branch,
            "path": sync_result.path,
            "error": sync_result.error,
        }
        if not sync_result.ok:
            raise RuntimeError(f"mirror sync failed: {sync_result.error}")

    repo_path = resolve_repo_path(repo_cfg)
    if not after:
        after = _git_rev(repo_path, "HEAD")
    if not before or before == _ZERO_SHA:
        # Fall back to previous tip if possible.
        parent = _git_rev(repo_path, "HEAD~1")
        before = parent or before

    diff_text = ""
    if before and after and before != after:
        try:
            diff_text = get_diff_between_refs(repo_path, before, after)
        except RuntimeError as exc:
            logger.warning("merge diff failed for %s (%s..%s): %s", repo_cfg.name, before, after, exc)

    findings_count = 0
    packs_ids: list[str] = []
    files_changed = 0
    message = ""

    if diff_text.strip():
        result = run_diff_audit(repo_cfg, diff_text)
        files_changed = len(result.impact.directly_changed)
        findings_count = len(result.findings)
        packs = emit_packs_from_findings(
            result.findings,
            output_dir=out,
            repo_path=repo_path,
            kind="diff_audit",
            run_id=run_id,
            directly_changed=sorted(result.impact.directly_changed),
            transitively_affected=sorted(result.impact.transitively_affected),
            public_base_url=public_base_url(),
            ingest_rag=do_rag,
        )
        packs_ids.extend(p.work_item_id for p in packs)
    else:
        message = "empty diff — refreshed world pack only"

    world = emit_world_model_packs(
        output_dir=out,
        run_id=run_id,
        public_base_url=public_base_url(),
        ingest_rag=do_rag,
        include_ecosystem=False,
        include_memory=False,
        include_repos=True,
        repos=[repo_cfg.name],
    )
    packs_ids.extend(p.work_item_id for p in world)

    return {
        "repo": repo_cfg.name,
        "run_id": run_id,
        "before": before,
        "after": after,
        "sync": sync_info,
        "files_changed": files_changed,
        "findings": findings_count,
        "agent_packs": packs_ids,
        "ingest_rag": do_rag,
        "message": message,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


class MergeHookScheduler:
    """In-process debounce + serial worker for merge refresh jobs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: dict[str, MergeEvent] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._ready: deque[str] = deque()
        self._statuses: dict[str, MergeJobStatus] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=50)
        self._running_repo: str | None = None
        self._worker_thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": merge_hook_enabled(),
                "debounce_s": merge_debounce_s(),
                "global_gap_s": merge_global_gap_s(),
                "branch": merge_branch(),
                "running": self._running_repo,
                "pending": {
                    name: {
                        "before": ev.before,
                        "after": ev.after,
                        "source": ev.source,
                        "scheduled_for": (
                            self._statuses.get(name).scheduled_for
                            if self._statuses.get(name)
                            else None
                        ),
                    }
                    for name, ev in self._pending.items()
                },
                "ready": list(self._ready),
                "recent": list(self._history),
            }

    def enqueue(self, event: MergeEvent) -> dict[str, Any]:
        if not merge_hook_enabled() and not event.force:
            return {"accepted": False, "reason": "merge hook disabled", "repo": event.repo}

        repo = event.repo.strip()
        if not repo:
            return {"accepted": False, "reason": "empty repo", "repo": repo}

        delay = 0 if event.force else merge_debounce_s()
        with self._lock:
            existing = self._pending.get(repo)
            merged = existing.coalesce(event) if existing else event
            merged.repo = repo
            self._pending[repo] = merged
            run_at = time.time() + delay
            self._statuses[repo] = MergeJobStatus(
                repo=repo,
                state="pending",
                before=merged.before,
                after=merged.after,
                scheduled_for=run_at,
            )
            old = self._timers.pop(repo, None)
            if old is not None:
                old.cancel()
            if delay <= 0:
                self._arm_ready(repo)
            else:
                timer = threading.Timer(delay, self._on_debounce_fire, args=(repo,))
                timer.daemon = True
                self._timers[repo] = timer
                timer.start()

        return {
            "accepted": True,
            "repo": repo,
            "debounced_s": delay,
            "coalesced": existing is not None,
            "before": merged.before,
            "after": merged.after,
            "scheduled_for": datetime.fromtimestamp(run_at, tz=timezone.utc).isoformat(),
        }

    def _on_debounce_fire(self, repo: str) -> None:
        with self._lock:
            self._timers.pop(repo, None)
            if repo not in self._pending:
                return
            self._arm_ready(repo)

    def _arm_ready(self, repo: str) -> None:
        if repo not in self._ready:
            self._ready.append(repo)
        st = self._statuses.get(repo)
        if st:
            st.state = "pending"
        self._kick_worker()

    def _kick_worker(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        t = threading.Thread(target=self._worker_loop, name="merge-hook-worker", daemon=True)
        self._worker_thread = t
        t.start()

    def _worker_loop(self) -> None:
        gap = merge_global_gap_s()
        while True:
            with self._lock:
                if not self._ready:
                    self._running_repo = None
                    self._worker_thread = None
                    return
                repo = self._ready.popleft()
                event = self._pending.pop(repo, None)
                self._timers.pop(repo, None)
                self._running_repo = repo
                if event is None:
                    continue
                st = self._statuses.get(repo) or MergeJobStatus(repo=repo, state="running")
                st.state = "running"
                st.started_at = time.time()
                st.before = event.before
                st.after = event.after
                self._statuses[repo] = st

            try:
                result = run_merge_refresh(event, sync_mirror=event.sync_mirror)
                with self._lock:
                    st = self._statuses.get(repo) or MergeJobStatus(repo=repo, state="done")
                    st.state = "done"
                    st.finished_at = time.time()
                    st.result = result
                    self._statuses[repo] = st
                    self._history.appendleft({"repo": repo, "ok": True, **result})
                logger.info(
                    "merge refresh done for %s: findings=%s packs=%s",
                    repo,
                    result.get("findings"),
                    len(result.get("agent_packs") or []),
                )
            except Exception as exc:
                logger.exception("merge refresh failed for %s", repo)
                with self._lock:
                    st = self._statuses.get(repo) or MergeJobStatus(repo=repo, state="error")
                    st.state = "error"
                    st.finished_at = time.time()
                    st.error = str(exc)[:500]
                    self._statuses[repo] = st
                    self._history.appendleft(
                        {
                            "repo": repo,
                            "ok": False,
                            "error": st.error,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            if gap > 0:
                # Peek: if more work is queued, honour the inter-repo quiet window.
                with self._lock:
                    more = bool(self._ready)
                if more:
                    time.sleep(gap)


_SCHEDULER: MergeHookScheduler | None = None
_SCHEDULER_LOCK = threading.Lock()


def get_scheduler() -> MergeHookScheduler:
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = MergeHookScheduler()
        return _SCHEDULER
