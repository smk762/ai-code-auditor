"""Write finding classes onto packs (#53), so class packs (#54) can group them by the guard that
would eliminate them.

Only the classifier that met the gate writes: Haiku, Sonnet and Opus with the frozen, measured
prompt, and a class is written only when all three agree and the class has a guard. Everything
else stays unclassified. Each pack's answers are cached by a hash of its title and description in
``reports/_classes/pack_classes.json``, so a pack is paid for once and re-emits are free. A pack
whose text changed is asked again, and a class that no longer holds is removed.

The class goes on ``governance.finding_class`` as ``{id, guard, by, text_sha}`` and in INDEX.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from auditor import finding_class_llm as llm
from auditor.finding_class import UNCLASSIFIED, taxonomy

logger = logging.getLogger(__name__)

CACHE = "pack_classes.json"
NON_FINDING_KINDS = frozenset({"world_model", "architecture", "extraction", "invariants", "class_guard"})
BY = f"consensus:cls-{llm.PROMPT_VERSION}"


def enabled() -> bool:
    return os.getenv("AI_AUDIT_PACK_CLASSES", "1").strip().lower() not in {"0", "false", "no", "off"}


def text_sha(title: str, desc: str) -> str:
    return hashlib.sha256(f"{title}\n{desc}".encode()).hexdigest()[:16]


def _row(pack: dict[str, Any]) -> dict[str, Any]:
    nar = pack.get("narrative") or {}
    title, desc = str(nar.get("title") or ""), str(nar.get("description") or "")
    return {"id": pack["work_item_id"], "title": title, "desc": desc, "sha": text_sha(title, desc)}


def _load_cache(output_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        return json.loads((llm.classes_dir(output_dir) / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(output_dir: Path, cache: dict[str, dict[str, Any]]) -> None:
    p = llm.classes_dir(output_dir) / CACHE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")


def finding_packs(output_dir: Path) -> list[dict[str, Any]]:
    from auditor.agent_pack import _read_pack_yaml, packs_dir

    out = []
    for p in sorted(packs_dir(output_dir).glob("*.yaml")):
        d = _read_pack_yaml(p)
        if d and d.get("work_item_id") and d.get("kind") not in NON_FINDING_KINDS:
            out.append(d)
    return out


def decide(votes: dict[str, str]) -> str:
    """The class to write: the consensus, and only when that class has a guard."""
    tax = taxonomy()
    cls = llm.consensus([votes.get(m, UNCLASSIFIED) for m in llm.MODELS])
    c = tax.get(cls) if cls else None
    return cls if c and c.guard != "none" else UNCLASSIFIED


def run(output_dir: str | Path, *, dry_run: bool = False, mode: str = "batch", runner_factory: Any = None,
        public_base_url: str = "") -> dict[str, Any]:
    from auditor.agent_pack import AgentCodingPack, rebuild_index, write_pack

    out = Path(output_dir)
    system = llm.frozen_system(out)
    if system is None:
        return {"skipped": "no frozen prompt (run `ai-audit eval classes --llm` once where the gold set is)"}
    packs = finding_packs(out)
    cache = _load_cache(out)
    rows = [_row(p) for p in packs]
    todo = sorted((r for r in rows if (cache.get(r["id"]) or {}).get("sha") != r["sha"]), key=lambda r: r["id"])
    priced = [llm.classify_rows(todo, m, out, system=system, mode=mode, dry_run=True) for m in llm.MODELS] if todo else []
    est = round(sum(p["est_usd"] for p in priced), 4)
    if dry_run:
        return {"packs": len(rows), "to_classify": len(todo), "est_usd": est}
    cost = 0.0
    if todo:
        answers: dict[str, list[str]] = {}
        for m in llm.MODELS:
            res = llm.classify_rows(todo, m, out, system=system, mode=mode,
                                    runner=runner_factory() if runner_factory else None)
            cost += res.get("cost_usd") or 0.0
            answers[m] = res["preds"]
        for k, r in enumerate(todo):
            votes = {m: answers[m][k] for m in llm.MODELS}
            if any(not v for v in votes.values()):
                continue  # a failed request: ask again next run rather than cache a non-answer
            cache[r["id"]] = {"sha": r["sha"], "votes": votes, "class": decide(votes)}
        _save_cache(out, cache)
    tax = taxonomy()
    written = removed = 0
    by_class: dict[str, int] = {}
    for data, r in zip(packs, rows, strict=True):
        hit = cache.get(r["id"]) or {}
        cls = hit.get("class") if hit.get("sha") == r["sha"] else UNCLASSIFIED
        cur = ((data.get("governance") or {}).get("finding_class") or {})
        want = ({"id": cls, "guard": tax.get(cls).guard, "by": BY, "text_sha": r["sha"]}  # type: ignore[union-attr]
                if cls else None)
        if cls:
            by_class[cls] = by_class.get(cls, 0) + 1
        if (cur or None) == want:
            continue
        pack = AgentCodingPack.from_dict(data)
        if want:
            pack.governance["finding_class"] = want
            written += 1
        else:
            pack.governance.pop("finding_class", None)
            removed += 1
        write_pack(pack, out, preserve_status=False)
    if written or removed:
        rebuild_index(out, public_base_url=public_base_url)
    return {"packs": len(rows), "classified_now": len(todo), "est_usd": est, "cost_usd": round(cost, 4),
            "written": written, "removed": removed, "with_class": sum(by_class.values()), "by_class": by_class}


def run_quietly(output_dir: str | Path, **kw: Any) -> dict[str, Any]:
    """Nightly step: never fails the run."""
    if not enabled():
        return {"skipped": "AI_AUDIT_PACK_CLASSES=0"}
    try:
        return run(output_dir, **kw)
    except Exception as exc:  # a classification failure must not fail the audit
        logger.exception("pack classes failed")
        return {"error": str(exc)}
