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
NON_FINDING_KINDS = frozenset({"world_model", "architecture", "extraction", "invariants", "class_guard",
    "module_cycle", "private_import"})
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


def static_class(pack: dict[str, Any]) -> str:
    """A static tool's rule names the class outright (suite rules, #55): no model needed."""
    from auditor.finding_class import classify, static_rule_id

    prov = pack.get("provenance") or {}
    source = str(prov.get("source") or "")
    rid = static_rule_id(str(prov.get("finding_id") or ""), source)
    if not rid:
        return UNCLASSIFIED
    m = classify("", source=source, rule_id=rid)
    return m.cls if m.rule.startswith("static:") else UNCLASSIFIED


def _votes(entry: dict[str, Any] | None, sha: str) -> dict[str, str]:
    """The answers cached for this text (empty when the text changed since)."""
    return dict((entry or {}).get("votes") or {}) if (entry or {}).get("sha") == sha else {}


def run(output_dir: str | Path, *, dry_run: bool = False, mode: str = "batch", runner_factory: Any = None,
        public_base_url: str = "") -> dict[str, Any]:
    from auditor.agent_pack import AgentCodingPack, load_pack, pack_lock, rebuild_index, write_pack

    out = Path(output_dir)
    system = llm.frozen_system(out)
    packs = finding_packs(out)
    static = {d["work_item_id"]: static_class(d) for d in packs}
    if system is None and not any(static.values()):
        return {"skipped": "no frozen prompt (run `ai-audit eval classes --llm` once where the gold set is)"}
    cache = _load_cache(out)
    rows = [_row(p) for p in packs]
    todo = [] if system is None else sorted(
        (r for r in rows if not static[r["id"]]
         and ((cache.get(r["id"]) or {}).get("sha") != r["sha"] or "class" not in (cache.get(r["id"]) or {}))),
        key=lambda r: r["id"])
    # Per model, only the rows it hasn't answered yet: answers are cached as each model returns,
    # so a run that fails part-way keeps what it already paid for.
    need = {m: [r for r in todo if not _votes(cache.get(r["id"]), r["sha"]).get(m)] for m in llm.MODELS}
    priced = [llm.classify_rows(need[m], m, out, system=system, mode=mode, dry_run=True)
              for m in llm.MODELS if need[m]]
    est = round(sum(p["est_usd"] for p in priced), 4)
    if dry_run:
        return {"packs": len(rows), "to_classify": len(todo), "est_usd": est}
    cost = 0.0
    if todo:
        for m in llm.MODELS:
            if not need[m]:
                continue
            res = llm.classify_rows(need[m], m, out, system=system, mode=mode,
                                    runner=runner_factory() if runner_factory else None)
            cost += res.get("cost_usd") or 0.0
            preds = list(res.get("preds") or [])
            if len(preds) != len(need[m]):
                logger.warning("pack classes: %s returned %d answers for %d packs; asking again next run",
                               m, len(preds), len(need[m]))
                continue
            for r, v in zip(need[m], preds, strict=True):
                if not v:
                    continue  # a failed request: ask again next run rather than cache a non-answer
                votes = _votes(cache.get(r["id"]), r["sha"])
                votes[m] = v
                cache[r["id"]] = {"sha": r["sha"], "votes": votes}
            _save_cache(out, cache)
        for r in todo:
            votes = _votes(cache.get(r["id"]), r["sha"])
            if votes and all(votes.get(m) for m in llm.MODELS):
                cache[r["id"]] = {"sha": r["sha"], "votes": votes, "class": decide(votes)}
        _save_cache(out, cache)
    tax = taxonomy()
    written = removed = 0
    by_class: dict[str, int] = {}
    # The model calls above can take hours: each pack is re-read under the pack lock and only its
    # finding_class changes, so a status or edit made meanwhile is never overwritten.
    with pack_lock(out):
        for r in rows:
            data = load_pack(out, r["id"])
            if data is None or _row(data)["sha"] != r["sha"]:
                continue  # gone, or its text changed since: the next run classifies the new text
            hit = cache.get(r["id"]) or {}
            by = BY
            if static[r["id"]]:
                cls, by = static[r["id"]], f"static:{(data.get('provenance') or {}).get('source', '')}"
            else:
                cls = hit.get("class") if hit.get("sha") == r["sha"] else UNCLASSIFIED
            cur = ((data.get("governance") or {}).get("finding_class") or {})
            want = ({"id": cls, "guard": tax.get(cls).guard, "by": by, "text_sha": r["sha"]}  # type: ignore[union-attr]
                    if cls and tax.get(cls) else None)
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
