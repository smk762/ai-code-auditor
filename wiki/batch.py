"""Run model requests through the Message Batches API (50% price), safely across restarts.

Each request's ``custom_id`` is derived from a hash of its exact parameters, and every finished
result is written to ``reports/wiki/_results/<custom_id>.json`` as soon as it is harvested.
So a rerun never pays twice for the same request: finished results are reused, submitted but
unharvested batches (``_batches.json``) are harvested instead of resubmitted, and a changed input
gets a new id. Spend is priced before submitting, checked against the ``wiki`` lane, and the
real usage is recorded in the spend ledger when results arrive.

``mode="sync"`` sends the same requests one by one through the streaming Messages API (full
price): useful for a quick look at one repo without waiting for a batch.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auditor.spend_ledger import BudgetExceeded, cost_usd, get_ledger

logger = logging.getLogger(__name__)

LANE = "wiki"
CHARS_PER_TOKEN = 3.5
MAX_REQUESTS_PER_BATCH = 2000


def request_id(prefix: str, params: dict[str, Any]) -> str:
    """Stable id for these exact params (batch custom_ids allow [a-zA-Z0-9_-]{1,64})."""
    digest = hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:20]
    return f"{prefix[:40]}-{digest}"


def estimate_usd(params: dict[str, Any], output_tokens: int, *, batch: bool = True) -> float:
    chars = len(json.dumps(params.get("system", ""), default=str)) + len(json.dumps(params["messages"], default=str))
    return cost_usd(params["model"], int(chars / CHARS_PER_TOKEN), output_tokens, batch=batch)


@dataclass
class Request:
    custom_id: str
    params: dict[str, Any]
    est_usd: float
    note: str  # spend-ledger note, e.g. "wiki:argus-cortex"


@dataclass
class Result:
    custom_id: str
    text: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    cost: float = 0.0

    @property
    def ok(self) -> bool:
        return self.text is not None and self.error is None and self.stop_reason == "end_turn"


@dataclass
class StageStats:
    requested: int = 0
    reused: int = 0
    submitted: int = 0
    est_usd: float = 0.0
    cost_usd: float = 0.0
    failed: int = 0
    batch_ids: list[str] = field(default_factory=list)


class Runner:
    def __init__(self, output_dir: str | Path, *, mode: str = "batch", client: Any = None,
                 poll_s: int = 30, max_wait_s: int = 6 * 3600, concurrency: int = 4):
        if mode not in ("batch", "sync"):
            raise ValueError(f"mode must be batch or sync, not {mode!r}")
        self.root = Path(output_dir) / "wiki"
        self.results_dir = self.root / "_results"
        self.state_path = self.root / "_batches.json"
        self.mode, self.poll_s, self.max_wait_s, self.concurrency = mode, poll_s, max_wait_s, concurrency
        self._client = client

    # ── plumbing ─────────────────────────────────────────────────────────────
    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(max_retries=4)
        return self._client

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"batches": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def _result_path(self, custom_id: str) -> Path:
        return self.results_dir / f"{custom_id}.json"

    def _stored(self, custom_id: str) -> Result | None:
        try:
            return Result(**json.loads(self._result_path(custom_id).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _store(self, r: Result) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._result_path(r.custom_id).write_text(json.dumps(r.__dict__), encoding="utf-8")

    def _record(self, custom_id: str, msg: Any, note: str, *, batch: bool) -> Result:
        cost = get_ledger().record_usage(LANE, getattr(msg, "model", ""), msg.usage, batch=batch, note=note)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        r = Result(custom_id, text=text, stop_reason=msg.stop_reason, cost=cost)
        self._store(r)
        return r

    def forget(self, custom_ids: list[str]) -> None:
        """Delete stored results once their output is written (keeps _results small)."""
        for cid in custom_ids:
            self._result_path(cid).unlink(missing_ok=True)

    # ── harvesting ───────────────────────────────────────────────────────────
    def _harvest(self, batch_id: str, notes: dict[str, str], stats: StageStats) -> bool:
        deadline = time.monotonic() + self.max_wait_s
        while True:
            b = self.client.messages.batches.retrieve(batch_id)
            if b.processing_status == "ended":
                break
            if time.monotonic() > deadline:
                logger.warning("batch %s still %s after %ss; a rerun will harvest it",
                               batch_id, b.processing_status, self.max_wait_s)
                return False
            logger.info("batch %s: %s (%d processing)", batch_id, b.processing_status, b.request_counts.processing)
            time.sleep(self.poll_s)
        for entry in self.client.messages.batches.results(batch_id):
            cid, res = entry.custom_id, entry.result
            if res.type == "succeeded":
                r = self._record(cid, res.message, notes.get(cid, "wiki"), batch=True)
                stats.cost_usd += r.cost
            else:
                err = getattr(getattr(res, "error", None), "type", None) or res.type
                self._store(Result(cid, error=f"batch result {res.type}: {err}"))
        state = self._load_state()
        state["batches"].pop(batch_id, None)
        self._save_state(state)
        return True

    def harvest_pending(self) -> int:
        """Harvest batches submitted by an earlier, interrupted run. Returns how many."""
        done = 0
        for batch_id, info in list(self._load_state()["batches"].items()):
            if self._harvest(batch_id, info.get("notes", {}), StageStats()):
                done += 1
        return done

    # ── the one entry point ──────────────────────────────────────────────────
    def run(self, stage: str, requests: list[Request]) -> tuple[dict[str, Result], StageStats]:
        stats = StageStats(requested=len(requests))
        if self.mode == "batch":
            self.harvest_pending()
        results: dict[str, Result] = {}
        todo: list[Request] = []
        for req in requests:
            prior = self._stored(req.custom_id)
            if prior is not None and prior.error is None:
                results[req.custom_id] = prior
                stats.reused += 1
            else:
                todo.append(req)
        stats.est_usd = sum(r.est_usd for r in todo) * (1 if self.mode == "batch" else 2)
        if not todo:
            return results, stats
        left = get_ledger().remaining(LANE)
        if stats.est_usd > left:
            raise BudgetExceeded(f"wiki {stage}: estimated ${stats.est_usd:.2f} for {len(todo)} requests, "
                                 f"but the wiki lane has ${left:.2f} left this budget month")
        stats.submitted = len(todo)
        logger.info("wiki %s: %d requests (~$%.2f), %d reused", stage, len(todo), stats.est_usd, stats.reused)
        if self.mode == "sync":
            self._run_sync(todo, stats)
        else:
            self._run_batch(stage, todo, stats)
        for req in requests:
            if req.custom_id not in results:
                r = self._stored(req.custom_id) or Result(req.custom_id, error="no result (batch not finished?)")
                results[req.custom_id] = r
        stats.failed = sum(1 for r in results.values() if not r.ok)
        return results, stats

    def _run_batch(self, stage: str, todo: list[Request], stats: StageStats) -> None:
        notes = {r.custom_id: r.note for r in todo}
        for i in range(0, len(todo), MAX_REQUESTS_PER_BATCH):
            chunk = todo[i:i + MAX_REQUESTS_PER_BATCH]
            batch = self.client.messages.batches.create(
                requests=[{"custom_id": r.custom_id, "params": r.params} for r in chunk])
            state = self._load_state()
            state["batches"][batch.id] = {"stage": stage, "submitted_at": time.time(),
                                          "notes": {r.custom_id: r.note for r in chunk}}
            self._save_state(state)
            stats.batch_ids.append(batch.id)
            logger.info("wiki %s: submitted batch %s (%d requests)", stage, batch.id, len(chunk))
        for batch_id in stats.batch_ids:
            self._harvest(batch_id, notes, stats)

    def _run_sync(self, todo: list[Request], stats: StageStats) -> None:
        def one(req: Request) -> Result:
            try:
                with self.client.messages.stream(**req.params) as stream:
                    msg = stream.get_final_message()
            except Exception as exc:  # noqa: BLE001 — recorded per request, the stage goes on
                r = Result(req.custom_id, error=f"{type(exc).__name__}: {exc}"[:500])
                self._store(r)
                return r
            return self._record(req.custom_id, msg, req.note, batch=False)

        with ThreadPoolExecutor(self.concurrency) as pool:
            for r in pool.map(one, todo):
                stats.cost_usd += r.cost
