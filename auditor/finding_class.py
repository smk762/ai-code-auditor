"""Finding classes (#53): which class of mistake a finding is, so the class can be eliminated.

Rules first, from ``config/finding_classes.yaml``:

1. A static tool's rule id (bandit ``B113``, a semgrep check id) maps directly.
2. Otherwise the title patterns are tried in file order. Classes are ordered most specific
   first, and the first match wins.

Anything no rule matches stays unclassified (``""``). That's deliberate: a wrong class sends a
guard after the wrong thing, so precision matters more than coverage. An LLM fallback for the
rest is a later, opt-in step.

``evaluate()`` measures the rules against a labelled gold set kept outside the repo (it quotes
real findings): ``~/.config/ai-code-auditor/eval/finding_classes_*.jsonl``, or
``AI_AUDIT_EVAL_DIR``. Each line is ``{"id", "title", "desc", "label", "set": "dev"|"test"}``.
Only the ``test`` set, labelled blind after the rules were written, counts toward the gate.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "finding_classes.yaml"
EVAL_NAME = "eval_classes.json"
UNCLASSIFIED = ""


@dataclass
class FindingClass:
    id: str
    description: str
    guard: str
    cwe: int | None = None
    title: list[re.Pattern[str]] = field(default_factory=list)


@dataclass
class Taxonomy:
    classes: list[FindingClass]
    static_rules: dict[str, dict[str, str]]

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.classes]

    def get(self, cid: str) -> FindingClass | None:
        return next((c for c in self.classes if c.id == cid), None)


def load_taxonomy(path: str | Path = CONFIG) -> Taxonomy:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    classes = []
    for c in data.get("classes") or []:
        classes.append(FindingClass(
            id=str(c["id"]), description=str(c.get("description") or ""), guard=str(c.get("guard") or "none"),
            cwe=c.get("cwe"), title=[re.compile(p, re.IGNORECASE) for p in c.get("title") or []],
        ))
    ids = [c.id for c in classes]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate class ids in {path}")
    static = {str(tool): {str(k): str(v) for k, v in (m or {}).items()}
              for tool, m in (data.get("static_rules") or {}).items()}
    for tool, m in static.items():
        unknown = set(m.values()) - set(ids)
        if unknown:
            raise ValueError(f"static_rules.{tool} maps to unknown classes: {sorted(unknown)}")
    return Taxonomy(classes=classes, static_rules=static)


@lru_cache(maxsize=4)
def _default(path: str) -> Taxonomy:
    return load_taxonomy(path)


def taxonomy() -> Taxonomy:
    return _default(str(CONFIG))


@dataclass
class Match:
    cls: str          # class id, or "" when unclassified
    rule: str         # "static:bandit:B113" | "title:<pattern>" | ""


def classify(title: str, *, source: str = "", rule_id: str = "", tax: Taxonomy | None = None) -> Match:
    """The class of one finding. *source* is the finding's ``source`` (``bandit``, ``semgrep``,
    ``llm``…) and *rule_id* the static tool's check id, when there is one."""
    tax = tax or taxonomy()
    tool = (source or "").lower()
    if rule_id and tool in tax.static_rules:
        rid = rule_id.split(".")[-1]
        for key in (rule_id, rid):
            if key in tax.static_rules[tool]:
                return Match(tax.static_rules[tool][key], f"static:{tool}:{key}")
    text = title or ""
    for c in tax.classes:
        for pat in c.title:
            if pat.search(text):
                return Match(c.id, f"title:{pat.pattern}")
    return Match(UNCLASSIFIED, "")


def static_rule_id(finding_id: str, source: str) -> str:
    """The check id embedded in a static finding's id (``bandit:B113:...`` → ``B113``)."""
    parts = (finding_id or "").split(":")
    return parts[1] if len(parts) > 2 and parts[0].lower() == (source or "").lower() else ""


# ── gold-set evaluation ──────────────────────────────────────────────────────

def eval_dir() -> Path:
    return Path(os.getenv("AI_AUDIT_EVAL_DIR") or Path.home() / ".config" / "ai-code-auditor" / "eval")


def load_gold(directory: Path | None = None, *, reviewed: bool = True) -> list[dict[str, Any]]:
    """Gold rows. With *reviewed*, a label a second labeller adjudicated replaces the first
    (``finding_class_review``)."""
    from auditor import finding_class_review as review

    directory = directory or eval_dir()
    rows: list[dict[str, Any]] = []
    for p in sorted(directory.glob("finding_classes_*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return review.apply_reviews(rows, review.load_reviews(directory)) if reviewed else rows


def evaluate(rows: list[dict[str, Any]], *, tax: Taxonomy | None = None,
             predict: Any = None) -> dict[str, Any]:
    """Precision and coverage of a classifier, per set (dev / test) and per class. *predict*
    maps a gold row to a class id ("" = unclassified); the default is the title rules.

    Precision = right / classified (unclassified rows don't count against it).
    Coverage = classified / all rows."""
    tax = tax or taxonomy()
    out: dict[str, Any] = {"sets": {}, "classes": tax.ids}
    for s in sorted({r.get("set", "dev") for r in rows}):
        sub = [r for r in rows if r.get("set", "dev") == s]
        per: dict[str, dict[str, int]] = {}
        wrong: list[dict[str, str]] = []
        classified = right = 0
        for r in sub:
            m = classify(r["title"], tax=tax) if predict is None else Match(predict(r), "llm")
            gold = r["label"]
            cell = per.setdefault(gold, {"n": 0, "found": 0})
            cell["n"] += 1
            if not m.cls:
                continue
            classified += 1
            pred = per.setdefault(m.cls, {"n": 0, "found": 0})
            pred["predicted"] = pred.get("predicted", 0) + 1
            if m.cls == gold:
                right += 1
                cell["found"] += 1
                pred["right"] = pred.get("right", 0) + 1
            else:
                wrong.append({"id": r.get("id", ""), "title": r["title"][:120], "gold": gold, "got": m.cls,
                              "rule": m.rule})
        out["sets"][s] = {
            "n": len(sub), "classified": classified, "right": right,
            "precision": round(right / classified, 4) if classified else None,
            "coverage": round(classified / len(sub), 4) if sub else None,
            "per_class": {k: {**v, "recall": round(v["found"] / v["n"], 3) if v["n"] else None,
                              "precision": round(v.get("right", 0) / v["predicted"], 3) if v.get("predicted") else None}
                          for k, v in sorted(per.items())},
            "wrong": wrong,
        }
    return out
