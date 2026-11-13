"""Pre-fill the review cache through the Anthropic Message Batches API (50% price).

Used by `ai-audit repo-run --batch`: every uncached unit across all repos is submitted as one
or more batches; results land in the ReviewCache, so the normal per-unit pass that follows is
all cache hits. Batch ids are recorded in the cache DB, so if this process dies mid-wait the
next run harvests the finished batch instead of paying again.
"""
from __future__ import annotations

import logging
import os
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

logger = logging.getLogger(__name__)

# $ per 1M tokens (input, output), standard rate; batches bill at half.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
MAX_REQUESTS_PER_BATCH = 5000  # API allows 100k / 256 MB; smaller batches finish sooner


@dataclass
class BatchStats:
    units: int = 0
    already_cached: int = 0
    submitted: int = 0
    cached_new: int = 0
    parse_failed: int = 0
    errored: int = 0
    cost_usd: float = 0.0
    batch_ids: list[str] = field(default_factory=list)


def _cost(model: str, usage: Any) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (usage.input_tokens * pin + usage.output_tokens * pout) / 1e6 * 0.5


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

    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            stats.errored += 1
            continue
        msg = result.result.message
        stats.cost_usd += _cost(model, msg.usage)
        text = "".join(b.text for b in msg.content if b.type == "text")
        unit = units_by_id.get(result.custom_id)
        if unit is None:
            # Harvesting a batch from an earlier run: custom_id is the cache key itself.
            if _parse_findings_json(text, _placeholder_unit()) is not None:
                cache.put(result.custom_id, model, text)
                stats.cached_new += 1
            continue
        if _parse_findings_json(text, unit) is None:
            stats.parse_failed += 1
            continue
        cache.put(result.custom_id, model, text)
        stats.cached_new += 1
    cache.mark_harvested(batch_id)
    return True


def _placeholder_unit() -> CodeUnit:
    return CodeUnit(id="x", repo="", file_path="", symbol="", kind="", language="",
                    start_line=0, end_line=0, raw_text="")


def prefetch_reviews(repos: list[RepoConfig], *, poll_s: int = 60, max_wait_s: int | None = None) -> BatchStats:
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
    max_wait_s = max_wait_s or int(os.getenv("AI_AUDIT_LLM_BATCH_MAX_WAIT_S", str(6 * 3600)))

    import anthropic

    client = anthropic.Anthropic(max_retries=4)
    model = probe.model

    # Finish anything an earlier interrupted run left behind.
    for batch_id in cache.pending_batches():
        logger.info("harvesting batch %s from an earlier run", batch_id)
        _harvest(client, cache, batch_id, {}, model, stats, poll_s, max_wait_s)

    requests: list[dict[str, Any]] = []
    units_by_id: dict[str, CodeUnit] = {}
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
                if cache.has(key) or key in units_by_id:
                    stats.already_cached += 1
                    continue
                units_by_id[key] = unit
                params: dict[str, Any] = {
                    "model": model,
                    "max_tokens": 8192,
                    "messages": [{"role": "user", "content": llm.build_prompt(unit, repo_path=str(root))}],
                }
                if llm.effort:
                    params["output_config"] = {"effort": llm.effort}
                requests.append({"custom_id": key, "params": params})

    stats.submitted = len(requests)
    logger.info("batch prefetch: %d units, %d cached, %d to review with %s",
                stats.units, stats.already_cached, stats.submitted, model)
    for i in range(0, len(requests), MAX_REQUESTS_PER_BATCH):
        chunk = requests[i : i + MAX_REQUESTS_PER_BATCH]
        batch = client.messages.batches.create(requests=chunk)
        cache.add_batch(batch.id, model)
        stats.batch_ids.append(batch.id)
        logger.info("submitted batch %s (%d requests)", batch.id, len(chunk))
    for batch_id in stats.batch_ids:
        _harvest(client, cache, batch_id, units_by_id, model, stats, poll_s, max_wait_s)

    logger.info("batch prefetch done: %d new reviews cached, %d parse failures, %d errored, ~$%.2f",
                stats.cached_new, stats.parse_failed, stats.errored, stats.cost_usd)
    return stats
