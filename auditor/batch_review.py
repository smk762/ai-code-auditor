"""Pre-fill the review cache through the Anthropic Message Batches API (50% price).

Used by `ai-audit repo-run --batch`: every uncached unit across all repos is submitted as one
or more batches; results land in the ReviewCache, so the normal per-unit pass that follows
(run cache-only) is all cache hits. Batch ids are recorded in the cache DB, so if this process
dies mid-wait the next run harvests the finished batch instead of paying again.

Spend: requests are priced up front and trimmed to what the `audit_batch` lane has left this
budget month (security-relevant units first); actual usage is recorded in the spend ledger.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from auditor.chunker import extract_code_units
from auditor.config import RepoConfig
from auditor.contracts import CodeUnit
from auditor.ecosystem_briefing import build_repo_briefing
from auditor.llm_cache import ReviewCache
from auditor.llm_client import LLMClient, _parse_findings_json
from auditor.repo_resolver import resolve_scan_root
from auditor.repo_scanner import scan_repo
from auditor.spend_ledger import PRICES, cost_usd, get_ledger

logger = logging.getLogger(__name__)

LANE = "audit_batch"
MAX_REQUESTS_PER_BATCH = 5000  # API allows 100k / 256 MB; smaller batches finish sooner
# Output tokens per review (incl. thinking) measured in the 2026-11-13 evals with the v2 prompt.
EST_OUTPUT_TOKENS = {"claude-haiku-5-5": 1100, "claude-sonnet-5-5": 800, "claude-opus-5-5": 700}
CHARS_PER_TOKEN = 3.5
RISKY = re.compile(
    r"auth|token|jwt|session|passw|secret|permission|role|admin|owner|tenant|user_id|"
    r"sql|execute\(|query|subprocess|shell|open\(|upload|path|request\.|@app\.|@router|"
    r"router\.|Depends\(|verify|signature|presign|redirect",
    re.IGNORECASE,
)


@dataclass
class BatchStats:
    units: int = 0
    already_cached: int = 0
    skipped_oversize: int = 0
    candidates: int = 0
    submitted: int = 0
    deferred_for_budget: int = 0
    estimated_usd: float = 0.0
    cached_new: int = 0
    parse_failed: int = 0
    errored: int = 0
    cost_usd: float = 0.0
    batch_ids: list[str] = field(default_factory=list)


def estimate_usd(model: str, prompt: str) -> float:
    in_tokens = int(len(prompt) / CHARS_PER_TOKEN)
    return cost_usd(model, in_tokens, EST_OUTPUT_TOKENS.get(model, 1000), batch=True)


def _harvest(client: Any, cache: ReviewCache, batch_id: str, units_by_id: dict[str, CodeUnit],
             model: str, stats: BatchStats, poll_s: int, max_wait_s: int) -> bool:
    """Wait for a batch, write parseable results to the cache. False if it didn't finish in time."""
    deadline = time.monotonic() + max_wait_s
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        if time.monotonic() > deadline:
            logger.warning("batch %s still %s after %ss; will harvest next run",
                           batch_id, batch.processing_status, max_wait_s)
            return False
        logger.info("batch %s: %s (%d processing)", batch_id, batch.processing_status,
                    batch.request_counts.processing)
        time.sleep(poll_s)

    ledger = get_ledger()
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            stats.errored += 1
            continue
        msg = result.result.message
        unit = units_by_id.get(result.custom_id) or _placeholder_unit()
        stats.cost_usd += ledger.record_usage(LANE, getattr(msg, "model", model) or model,
                                              msg.usage, batch=True, note=unit.repo)
        text = "".join(b.text for b in msg.content if b.type == "text")
        if _parse_findings_json(text, unit) is None:
            stats.parse_failed += 1
            continue
        # custom_id is the cache key, so batches from earlier runs harvest correctly too.
        cache.put(result.custom_id, model, text)
        stats.cached_new += 1
    cache.mark_harvested(batch_id)
    return True


def _placeholder_unit() -> CodeUnit:
    return CodeUnit(id="x", repo="", file_path="", symbol="", kind="", language="",
                    start_line=0, end_line=0, raw_text="")


def prefetch_reviews(repos: list[RepoConfig], *, poll_s: int = 60, max_wait_s: int | None = None,
                     dry_run: bool = False) -> BatchStats:
    """Review every uncached unit via the Batches API. No-op unless the provider is anthropic."""
    stats = BatchStats()
    probe = LLMClient()
    if probe.provider != "anthropic":
        logger.info("batch prefetch skipped: provider is %s, not anthropic", probe.provider)
        return stats
    cache = probe.cache
    if cache is None:
        logger.info("batch prefetch skipped: review cache disabled")
        return stats
    model = probe.model
    if model not in PRICES:
        raise ValueError(f"no price for {model}; add it to auditor/spend_ledger.PRICES")
    max_wait_s = max_wait_s or int(os.getenv("AI_AUDIT_LLM_BATCH_MAX_WAIT_S", str(6 * 3600)))
    max_chars = int(os.getenv("AI_AUDIT_LLM_MAX_UNIT_CHARS", "60000"))

    client = None
    if not dry_run:
        import anthropic

        client = anthropic.Anthropic(max_retries=4)
        # Finish anything an earlier interrupted run left behind.
        for batch_id in cache.pending_batches():
            logger.info("harvesting batch %s from an earlier run", batch_id)
            _harvest(client, cache, batch_id, {}, model, stats, poll_s, max_wait_s)

    candidates: list[tuple[bool, float, str, dict[str, Any], CodeUnit]] = []
    seen: set[str] = set()
    for repo in repos:
        if not repo.enabled:
            continue
        try:
            root = resolve_scan_root(repo)
            files = scan_repo(repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("batch prefetch: skip %s (%s)", repo.name, exc)
            continue
        llm = LLMClient(provider_config=probe.provider_config,
                        repo_briefing=build_repo_briefing(repo.name, root))
        for f in files:
            for unit in extract_code_units(repo.name, f):
                stats.units += 1
                key = llm.cache_key(unit)
                if key in seen or cache.has(key):
                    stats.already_cached += 1
                    continue
                seen.add(key)
                if len(unit.raw_text) > max_chars:
                    stats.skipped_oversize += 1
                    continue
                prompt = llm.build_prompt(unit, repo_path=str(root))
                params: dict[str, Any] = {
                    "model": model,
                    "max_tokens": 8192,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if llm.effort:
                    params["output_config"] = {"effort": llm.effort}
                candidates.append((bool(RISKY.search(unit.raw_text)), estimate_usd(model, prompt),
                                   key, params, unit))
    stats.candidates = len(candidates)

    # Fit to the lane budget: security-relevant units first, then smallest first.
    budget = get_ledger().remaining(LANE)
    candidates.sort(key=lambda c: (not c[0], c[1]))
    chosen, spend = [], 0.0
    for c in candidates:
        if spend + c[1] > budget:
            stats.deferred_for_budget += 1
            continue
        chosen.append(c)
        spend += c[1]
    stats.estimated_usd = spend
    logger.info(
        "batch prefetch (%s, effort=%s): %d units, %d cached, %d oversize skipped, %d to review "
        "(~$%.2f est.), %d deferred for budget ($%.2f left in lane)",
        model, probe.effort, stats.units, stats.already_cached, stats.skipped_oversize,
        len(chosen), spend, stats.deferred_for_budget, budget,
    )
    if dry_run or not chosen:
        return stats

    units_by_id = {c[2]: c[4] for c in chosen}
    requests = [{"custom_id": c[2], "params": c[3]} for c in chosen]
    stats.submitted = len(requests)
    for i in range(0, len(requests), MAX_REQUESTS_PER_BATCH):
        chunk = requests[i : i + MAX_REQUESTS_PER_BATCH]
        batch = client.messages.batches.create(requests=chunk)
        cache.add_batch(batch.id, model)
        stats.batch_ids.append(batch.id)
        logger.info("submitted batch %s (%d requests)", batch.id, len(chunk))
    for batch_id in stats.batch_ids:
        _harvest(client, cache, batch_id, units_by_id, model, stats, poll_s, max_wait_s)

    logger.info("batch prefetch done: %d new reviews cached, %d parse failures, %d errored, $%.2f actual",
                stats.cached_new, stats.parse_failed, stats.errored, stats.cost_usd)
    return stats
