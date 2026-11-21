"""Label council (#53): three models check each held-out gold label, so the gold set doesn't rest
on one labeller.

The council is made of the same models the classifier eval grades, so it is kept apart from them
in three ways:

- **A different question.** It judges whether the given label fits the finding; it doesn't
  classify from scratch.
- **More evidence.** Each member sees the pack's full description, recommendation and cited code.
  The classifier sees only the title and a trimmed description.
- **A higher bar to change anything.** A label is replaced only when at least two members reject
  it and at least two name the same replacement. Otherwise the original stands, and a rejection
  without an agreed replacement is recorded as ``contested``.

Each result is appended to ``review_classes.jsonl`` as a ``council`` entry (with ``final`` when
the label changed), so ``load_gold()`` uses it. A summary goes to ``council_classes.json`` in the
eval dir, which also stays out of the repo because it quotes findings. Scores measured against
these labels mean "agreement with a better-informed judge", not ground truth.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from auditor import finding_class_review as review
from auditor.finding_class import Taxonomy, eval_dir, taxonomy
from auditor.finding_class_llm import EXTRA_LABELS, LANE

PROMPT_VERSION = "v1"
CHUNK = 10
SNIPPET_LINES = 30
TEXT_CHARS = 1200
MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
SUMMARY_NAME = "council_classes.json"

_INSTRUCTIONS = """You audit labels in a gold set of code-review findings. Each finding was given one class of \
mistake, so that each class can later be eliminated with one guard (a lint rule, a shared helper, an import \
contract). Your job is to judge whether the given label is the best class for the finding.

How classes are chosen:
- By root mechanism, not consequence. A race that leaks a stream is race_condition; a crash on an empty list \
is unhandled_error; a value accepted unchecked is missing_validation even if it later crashes.
- Findings about a test that cannot detect what it claims are test_quality, whatever the test is about.
- logic_error is for plain logic bugs (wrong computation, condition or mapping) that fit no other class.
- Use the code, not only the title: titles can overstate or misname the problem.

For each finding answer "agree" if the label is the best class, "disagree" if another class is clearly better \
(name it in "better"), or "unclear" if the finding doesn't let you tell. Disagree only when you are confident: \
a borderline case between two reasonable classes is "agree".

Answer with JSON only, no prose: {"verdicts": [{"i": <number>, "verdict": "agree"|"disagree"|"unclear", \
"better": "<class id or empty>", "why": "<at most 15 words>"}, ...]} with one entry per finding."""


def build_system(tax: Taxonomy) -> str:
    lines = [_INSTRUCTIONS, "", "Classes:"]
    lines += [f"- {c.id}: {c.description}" for c in tax.classes]
    lines.append("- logic_error: plain logic bug that fits no other class")
    return "\n".join(lines)


def pack_context(pack_id: str, packs_dir: Path) -> dict[str, str]:
    p = packs_dir / f"{pack_id}.yaml"
    if not p.exists():
        return {}
    d = yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    loc, nar = d.get("location") or {}, d.get("narrative") or {}
    snippet = "\n".join(str(loc.get("code_snippet") or "").splitlines()[:SNIPPET_LINES])
    return {"file": str(loc.get("file_path") or ""), "snippet": snippet,
            "description": str(nar.get("description") or "")[:TEXT_CHARS],
            "recommendation": str(nar.get("recommendation") or "")[:TEXT_CHARS // 2]}


def _item(i: int, row: dict[str, Any], ctx: dict[str, str]) -> str:
    parts = [f"## Finding {i}", f"Label: {row['label']}", f"Title: {row['title']}"]
    desc = ctx.get("description") or str(row.get("desc") or "")
    if desc:
        parts.append(f"Description: {' '.join(desc.split())}")
    if ctx.get("recommendation"):
        parts.append(f"Recommendation: {' '.join(ctx['recommendation'].split())}")
    if ctx.get("snippet"):
        parts.append(f"Code ({ctx.get('file') or 'cited'}):\n```\n{ctx['snippet']}\n```")
    return "\n".join(parts)


def build_requests(rows: list[dict[str, Any]], model: str, system: str, packs_dir: Path) -> list[tuple[Any, list[int]]]:
    from wiki.batch import Request, estimate_usd, request_id

    out = []
    for start in range(0, len(rows), CHUNK):
        idx = list(range(start, min(start + CHUNK, len(rows))))
        user = "\n\n".join(_item(k, rows[i], pack_context(rows[i]["id"], packs_dir)) for k, i in enumerate(idx))
        params = {"model": model, "max_tokens": 8000, "system": system,
                  "messages": [{"role": "user", "content": user}], "output_config": {"effort": "medium"}}
        rid = request_id(f"council-{PROMPT_VERSION}-{model.replace('claude-', '')}", params)
        out.append((Request(rid, params, estimate_usd(params, 2500), note=f"classes:council:{PROMPT_VERSION}"), idx))
    return out


def parse_verdicts(text: str, n: int, valid: set[str]) -> list[dict[str, str] | None]:
    out: list[dict[str, str] | None] = [None] * n
    # Decode at each '{' in turn: a greedy brace match spans any prose or second JSON block
    # around the payload, fails to parse, and drops every verdict.
    decoder, text = json.JSONDecoder(), text or ""
    verdicts = None
    for brace in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text, brace.start())
        except ValueError:
            continue
        if isinstance(data, dict) and "verdicts" in data:
            verdicts = data["verdicts"]
            break
    if not isinstance(verdicts, list):
        return out
    for e in verdicts:
        try:
            i, v = int(e.get("i")), str(e.get("verdict") or "")
        except (TypeError, ValueError, AttributeError):
            continue
        if not 0 <= i < n or v not in {"agree", "disagree", "unclear"}:
            continue
        better = str(e.get("better") or "")
        out[i] = {"verdict": v, "better": better if better in valid else "", "why": str(e.get("why") or "")[:160]}
    return out


def decide(label: str, votes: list[dict[str, str] | None]) -> dict[str, Any]:
    """``confirmed`` (≥2 agree), ``relabelled`` (≥2 reject and ≥2 name one replacement),
    ``contested`` (≥2 reject, no agreed replacement), else ``split``."""
    got = [v for v in votes if v]
    agree = sum(v["verdict"] == "agree" for v in got)
    reject = [v for v in got if v["verdict"] == "disagree"]
    if agree >= 2:
        return {"outcome": "confirmed", "final": label}
    if len(reject) >= 2:
        top = Counter(v["better"] for v in reject if v["better"] and v["better"] != label).most_common(1)
        if top and top[0][1] >= 2:
            return {"outcome": "relabelled", "final": top[0][0]}
        return {"outcome": "contested", "final": label}
    return {"outcome": "split", "final": label}


def run(rows: list[dict[str, Any]], output_dir: str | Path, packs_dir: str | Path, *, models=MODELS,
        split: str = "test", mode: str = "batch", dry_run: bool = False, record: bool = True,
        runner_factory: Any = None, directory: Path | None = None) -> dict[str, Any]:
    from wiki.batch import Runner

    tax = taxonomy()
    valid = set(tax.ids) | EXTRA_LABELS
    sub = [r for r in rows if r.get("set", "dev") == split]
    system = build_system(tax)
    plans = {m: build_requests(sub, m, system, Path(packs_dir)) for m in models}
    est = sum(r.est_usd for reqs in plans.values() for r, _ in reqs) * (1 if mode == "batch" else 2)
    if dry_run:
        return {"n": len(sub), "requests": sum(len(p) for p in plans.values()), "est_usd": round(est, 4)}
    votes: dict[str, list[dict[str, str] | None]] = {r["id"]: [] for r in sub}
    cost, failed = 0.0, 0
    for model, reqs in plans.items():
        runner = (runner_factory or (lambda: Runner(output_dir, mode=mode, lane=LANE, subdir="_classes")))()
        results, stats = runner.run(f"council-{model}", [r for r, _ in reqs])
        cost += stats.cost_usd
        for req, idx in reqs:
            res = results.get(req.custom_id)
            parsed = parse_verdicts(res.text or "", len(idx), valid) if res is not None and res.ok else [None] * len(idx)
            failed += res is None or not res.ok
            for k, i in enumerate(idx):
                v = parsed[k]
                votes[sub[i]["id"]].append({**v, "model": model} if v else None)
    decisions = []
    for r in sub:
        d = decide(r["label"], votes[r["id"]])
        decisions.append({"id": r["id"], "label": r["label"], **d, "votes": votes[r["id"]]})
    outcomes = Counter(d["outcome"] for d in decisions)
    summary = {"prompt": PROMPT_VERSION, "models": list(models), "n": len(sub), "est_usd": round(est, 4),
               "cost_usd": round(cost, 4), "failed_requests": failed, "outcomes": dict(outcomes),
               "fidelity": round(outcomes["confirmed"] / len(sub), 4) if sub else None, "decisions": decisions}
    if record:
        directory = directory or eval_dir()
        prior = review.load_reviews(directory)
        for d in decisions:
            if (prior.get(d["id"]) or {}).get("council") == d["outcome"] and d["outcome"] != "relabelled":
                continue
            entry: dict[str, Any] = {"id": d["id"], "council": d["outcome"]}
            if d["outcome"] == "relabelled":
                entry["final"] = d["final"]
            review._append(directory, entry)
        (directory / SUMMARY_NAME).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
