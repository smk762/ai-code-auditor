"""Split a patch's changes into "provably equivalent", intended behaviour changes, and "trust me".

A "trust me" change claims (or looks like) a no-behaviour-change refactor but equivalence isn't
evident from the code. The model must give the smallest input that tells old and new apart; a
"trust me" change *with* such a counterexample is a refuted refactor, and blocks validate_patch
from auto-resolving a pack. Opt-in: ``semantic_check`` on the request or
``AI_AUDIT_VALIDATE_SEMANTIC=1``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 60_000
KINDS = {"equivalent", "behavior_change", "trust_me"}

EQUIVALENCE_INSTRUCTIONS = """You are reviewing a patch hunk by hunk. Classify every logical change:

- "equivalent": provably the same observable behaviour for every input (renames, extracting a \
function with identical body, reordering independent statements, dead-code removal). You must be \
able to say why in one line.
- "behavior_change": deliberately changes behaviour (a fix, a feature, a new parameter). Fine, but \
say what changes.
- "trust_me": presented as or looks like a refactor/cleanup, but equivalence depends on something \
not shown or not guaranteed (ordering, exceptions, None/empty inputs, concurrency, retries, \
mutation of shared objects, default values, float/int, iteration order).

For every "trust_me" give "counterexample": the smallest concrete input, state or interleaving for \
which old and new code observably differ (return value, exception, side effect, or order of side \
effects). If after trying you find none, write "" and say so in "reason".

Return only a JSON array. Each element: file, line (new-file line), kind, summary, reason, counterexample.
Return [] for an empty or non-code patch.

Patch:
```diff
{diff}
```"""


def enabled(requested: bool | None) -> bool:
    if requested is not None:
        return requested
    return os.getenv("AI_AUDIT_VALIDATE_SEMANTIC", "0").strip().lower() in {"1", "true", "yes", "on"}


def parse_changes(raw: str) -> list[dict[str, Any]] | None:
    """Classified changes from model output, or None when it isn't a JSON array."""
    from auditor.llm_client import _extract_json_array

    payload = _extract_json_array(raw or "")
    if not isinstance(payload, list):
        return None
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        out.append({
            "file": str(item.get("file", "")),
            "line": item.get("line"),
            "kind": kind if kind in KINDS else "trust_me",  # unknown labels are not a proof
            "summary": str(item.get("summary", "")),
            "reason": str(item.get("reason", "")),
            "counterexample": str(item.get("counterexample", "") or "").strip(),
        })
    return out


def classify_patch(diff_text: str, llm: Any = None) -> dict[str, Any]:
    """Classify *diff_text*. ``refuted`` lists trust-me changes that came with a counterexample.

    Never raises: on model or parse trouble returns ``{"ok": False, "error": ...}`` so the caller
    can decide; validate_patch treats that as "not checked", not as a failure.
    """
    if not diff_text.strip():
        return {"ok": True, "changes": [], "refuted": [], "unproven": []}
    truncated = len(diff_text) > MAX_DIFF_CHARS
    prompt = EQUIVALENCE_INSTRUCTIONS.replace("{diff}", diff_text[:MAX_DIFF_CHARS])
    try:
        if llm is None:
            from auditor.llm_client import LLMClient

            llm = LLMClient(lane=os.getenv("AI_AUDIT_SPEND_LANE", "audit"))
        raw = llm.generate(prompt)
    except Exception as exc:
        logger.warning("equivalence check failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    changes = parse_changes(raw)
    if changes is None:
        return {"ok": False, "error": "model output was not a JSON array", "raw": (raw or "")[:300]}
    trust = [c for c in changes if c["kind"] == "trust_me"]
    return {
        "ok": True,
        "truncated": truncated,
        "changes": changes,
        "refuted": [c for c in trust if c["counterexample"]],
        "unproven": [c for c in trust if not c["counterexample"]],
    }
