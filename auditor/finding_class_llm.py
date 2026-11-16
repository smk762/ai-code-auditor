"""LLM finding classifier (#53): the fallback the title rules turned out to need.

Title rules scored 84% precision and 32% coverage on the blind held-out set: the scanner phrases
findings too variously for regexes. This asks a model to pick one class per finding from the
same taxonomy (``config/finding_classes.yaml``), 20 findings per request, through the Message
Batches API (``wiki.batch.Runner``: stable request ids, so reruns never pay twice; priced and
checked against the ``classes`` spend lane before anything is sent).

The prompt is versioned (``PROMPT_VERSION``). Its few-shot examples come only from the dev gold
set, never the held-out set. Changing the prompt after seeing held-out results makes that set
dev data, so a fresh blind set is needed to measure again.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from auditor.finding_class import UNCLASSIFIED, Taxonomy, taxonomy

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
LANE = "classes"
CHUNK = 20
DESC_CHARS = 160
UNCLEAR = "unclear"
# Answers the model may give beyond the taxonomy's guardable classes. logic_error is a real label
# (plain logic bugs no guard can eliminate) that the instructions define, so it is a valid answer;
# it isn't a taxonomy entry because it has no title rule and no guard.
EXTRA_LABELS = frozenset({"logic_error"})

_INSTRUCTIONS = """You classify code-review findings into classes of mistake, so that each class can later be \
eliminated with one guard (a lint rule, a shared helper, an import contract) instead of fixing findings one by one.

Rules:
- Pick exactly one class id per finding from the list below.
- Classify by the root mechanism, not the consequence. A race that leaks a stream is race_condition; \
a crash on an empty list is unhandled_error; a value that is accepted unchecked is missing_validation \
even if it later crashes.
- Findings about a test that cannot detect what it claims are test_quality, whatever the test is about.
- logic_error is for plain logic bugs (wrong computation, wrong condition, wrong mapping) that fit no other class.
- If you genuinely cannot tell, answer "unclear". Precision matters more than coverage: a wrong class sends a \
guard after the wrong thing.

Answer with JSON only, no prose: {"labels": [{"i": <number>, "class": "<class id>"}, ...]} with one entry per finding."""


def build_system(tax: Taxonomy, examples: dict[str, str]) -> str:
    lines = [_INSTRUCTIONS, "", "Classes:"]
    for c in tax.classes:
        lines.append(f"- {c.id}: {c.description}")
    lines.append(f"- {UNCLEAR}: none of the above can be told from the text")
    if examples:
        lines += ["", "Examples (title → class):"]
        lines += [f"- {title} → {cid}" for cid, title in examples.items()]
    return "\n".join(lines)


def few_shot(rows: list[dict[str, Any]], tax: Taxonomy) -> dict[str, str]:
    """One example title per class, from the dev set only, chosen deterministically (shortest title)."""
    out: dict[str, str] = {}
    for cid in tax.ids:
        cands = sorted((r["title"] for r in rows if r.get("set", "dev") == "dev" and r["label"] == cid), key=len)
        if cands:
            out[cid] = cands[0]
    return out


def _item(i: int, row: dict[str, Any]) -> str:
    desc = " ".join(str(row.get("desc") or "").split())[:DESC_CHARS]
    return f"{i}. {row['title']}" + (f" — {desc}" if desc else "")


def build_requests(rows: list[dict[str, Any]], model: str, system: str) -> list[tuple[Any, list[int]]]:
    """``(Request, row indexes)`` per chunk of CHUNK findings."""
    from wiki.batch import Request, estimate_usd, request_id

    out = []
    for start in range(0, len(rows), CHUNK):
        idx = list(range(start, min(start + CHUNK, len(rows))))
        user = "Findings:\n" + "\n".join(_item(k, rows[i]) for k, i in enumerate(idx))
        params = {
            "model": model, "max_tokens": 4000, "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": "low"},
        }
        rid = request_id(f"cls-{PROMPT_VERSION}-{model.replace('claude-', '')}", params)
        out.append((Request(rid, params, estimate_usd(params, 600), note=f"classes:{PROMPT_VERSION}"), idx))
    return out


def parse_labels(text: str, n: int, valid: set[str]) -> list[str]:
    """Class per finding from the model's JSON; anything missing or invalid is unclassified."""
    labels = [UNCLASSIFIED] * n
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return labels
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return labels
    for e in data.get("labels") or []:
        try:
            i, c = int(e.get("i")), str(e.get("class") or "")
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < n and c in valid:
            labels[i] = c
    return labels


def classify_rows(rows: list[dict[str, Any]], model: str, output_dir: str | Path, *, examples_from: list[dict[str, Any]],
                  mode: str = "batch", dry_run: bool = False, runner: Any = None) -> dict[str, Any]:
    """Classify *rows* with *model*. Returns ``{"preds": [...], "est_usd", "cost_usd", "failed"}``;
    on a dry run, only the price."""
    from wiki.batch import Runner

    tax = taxonomy()
    system = build_system(tax, few_shot(examples_from, tax))
    reqs = build_requests(rows, model, system)
    est = sum(r.est_usd for r, _ in reqs) * (1 if mode == "batch" else 2)
    if dry_run:
        return {"model": model, "requests": len(reqs), "est_usd": round(est, 4), "preds": None}
    runner = runner or Runner(output_dir, mode=mode, lane=LANE, subdir="_classes")
    results, stats = runner.run(f"classify-{model}", [r for r, _ in reqs])
    preds = [UNCLASSIFIED] * len(rows)
    valid = set(tax.ids) | EXTRA_LABELS
    failed = 0
    for req, idx in reqs:
        res = results.get(req.custom_id)
        if res is None or not res.ok:
            failed += 1
            continue
        for k, lab in enumerate(parse_labels(res.text or "", len(idx), valid)):
            preds[idx[k]] = lab
    return {"model": model, "requests": len(reqs), "est_usd": round(est, 4), "cost_usd": round(stats.cost_usd, 4),
            "reused": stats.reused, "failed": failed, "preds": preds}


def predictor(rows: list[dict[str, Any]], preds: list[str]) -> Callable[[dict[str, Any]], str]:
    by_id = {r["id"]: p for r, p in zip(rows, preds, strict=True)}
    return lambda row: by_id.get(row["id"], UNCLASSIFIED)
