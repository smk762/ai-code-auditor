"""Second-labeller review of the finding-class gold set (#53).

With one labeller, nobody knows how much of a classifier's error is really label noise. This adds a
second labeller in two passes, and the models never take part:

1. **Blind:** the reviewer labels held-out findings without seeing the existing label or any model's
   answer. The order is a fixed shuffle, so it doesn't reveal which findings are disputed.
2. **Adjudicate:** wherever the two labels differ, the reviewer picks between them, shown as A and B
   in random order, so they aren't told which is theirs. They may also pick a third class.

Answers are appended to ``review_classes.jsonl`` in the eval dir, so either pass can stop and pick
up where it left off. The name deliberately doesn't match ``finding_classes_*.jsonl``. When the gold
set is loaded, an adjudicated ``final`` label replaces the original, so the gate measures against
labels two people settled.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auditor.finding_class import Taxonomy, eval_dir, taxonomy

REVIEW_NAME = "review_classes.jsonl"
UNCLEAR = "unclear"
EXTRA = ("logic_error",)          # a valid label with no taxonomy entry (no guard, no title rule)
DESC_SHOW = 700


def review_path(directory: Path | None = None) -> Path:
    return (directory or eval_dir()) / REVIEW_NAME


def load_reviews(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    """Latest blind label and adjudicated label per finding id."""
    p = review_path(directory)
    out: dict[str, dict[str, Any]] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        cell = out.setdefault(e["id"], {})
        if "second" in e:
            cell["second"] = e["second"]
        if "final" in e:
            cell["final"] = e["final"]
    return out


def apply_reviews(rows: list[dict[str, Any]], reviews: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows with the adjudicated label in ``label`` (the original kept as ``label_first``).
    A finding both labellers called unclear has no usable label and is dropped."""
    out = []
    for r in rows:
        final = (reviews.get(r["id"]) or {}).get("final")
        if final is None:
            out.append(r)
        elif final != UNCLEAR:
            out.append({**r, "label": final, "label_first": r["label"]})
    return out


def _append(directory: Path | None, entry: dict[str, Any]) -> None:
    p = review_path(directory)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**entry, "at": datetime.now(UTC).isoformat(timespec="seconds")}) + "\n")


def _order_key(rid: str) -> str:
    return hashlib.sha256(f"review:{rid}".encode()).hexdigest()


def blind_queue(rows: list[dict[str, Any]], reviews: dict[str, dict[str, Any]], split: str = "test") -> list[dict]:
    return sorted((r for r in rows if r.get("set", "dev") == split and "second" not in reviews.get(r["id"], {})),
                  key=lambda r: _order_key(r["id"]))


def disputes(rows: list[dict[str, Any]], reviews: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings whose two labels differ and that aren't adjudicated yet."""
    out = []
    for r in sorted(rows, key=lambda r: _order_key(r["id"])):
        rv = reviews.get(r["id"]) or {}
        if "second" in rv and "final" not in rv and rv["second"] != r["label"]:
            out.append(r)
    return out


def guardable(tax: Taxonomy) -> set[str]:
    return {c.id for c in tax.classes if c.guard != "none"}


def agreement(rows: list[dict[str, Any]], reviews: dict[str, dict[str, Any]], tax: Taxonomy | None = None) -> dict:
    """How far two labellers agree: raw, Cohen's kappa, and on guardable classes only (the column
    the gate cares about: does a guard go after this finding, and which one)."""
    tax = tax or taxonomy()
    pairs = [(r["label"], reviews[r["id"]]["second"]) for r in rows if "second" in reviews.get(r["id"], {})]
    n = len(pairs)
    if not n:
        return {"n": 0}
    agree = sum(a == b for a, b in pairs)
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in ca) / (n * n)
    po = agree / n
    g = guardable(tax)
    gpairs = [(a, b) for a, b in pairs if a in g or b in g]
    adjudicated = [r for r in rows if "final" in reviews.get(r["id"], {})]
    first_kept = sum(reviews[r["id"]]["final"] == r["label"] for r in adjudicated)
    return {
        "n": n, "agree": agree, "raw": round(po, 4),
        "kappa": round((po - pe) / (1 - pe), 4) if pe < 1 else None,
        "guard_n": len(gpairs), "guard_agree": sum(a == b for a, b in gpairs),
        "guard_raw": round(sum(a == b for a, b in gpairs) / len(gpairs), 4) if gpairs else None,
        "disputed": n - agree, "adjudicated": len(adjudicated), "first_label_kept": first_kept,
    }


# ── the interactive passes ───────────────────────────────────────────────────

def _choices(tax: Taxonomy) -> list[str]:
    return [*tax.ids, *EXTRA, UNCLEAR]


def _show(row: dict[str, Any], out: Callable[[str], None]) -> None:
    desc = " ".join(str(row.get("desc") or "").split())
    out(f"\n  {row['title']}")
    if desc:
        out(f"  {desc[:DESC_SHOW]}{'…' if len(desc) > DESC_SHOW else ''}")


def _menu(tax: Taxonomy, out: Callable[[str], None]) -> None:
    g = guardable(tax)
    out("")
    for i, cid in enumerate(_choices(tax), 1):
        c = tax.get(cid)
        hint = (c.description[:70] if c else "plain logic bug, no other class" if cid == "logic_error"
                else "can't tell from the text")
        out(f"  {i:>2}  {cid:<22} {'guard' if cid in g else '     '}  {hint}")


def _parse(ans: str, choices: list[str]) -> str | None:
    ans = ans.strip()
    if ans.isdigit() and 1 <= int(ans) <= len(choices):
        return choices[int(ans) - 1]
    if ans in choices:
        return ans
    if ans == "u":
        return UNCLEAR
    return None


def run_blind(rows: list[dict[str, Any]], *, directory: Path | None = None, limit: int | None = None,
              ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> int:
    """Label held-out findings blind. Returns how many were labelled this session."""
    tax = taxonomy()
    choices = _choices(tax)
    queue = blind_queue(rows, load_reviews(directory))
    if limit:
        queue = queue[:limit]
    out(f"{len(queue)} findings to label. Answer with a number or class id; u = unclear, "
        "m = show the classes again, s = skip, q = quit (progress is saved).")
    _menu(tax, out)
    done = 0
    for k, row in enumerate(queue, 1):
        out(f"\n[{k}/{len(queue)}]")
        _show(row, out)
        while True:
            ans = ask("  class> ").strip()
            if ans == "q":
                return done
            if ans == "m":
                _menu(tax, out)
                continue
            if ans == "s":
                break
            label = _parse(ans, choices)
            if label:
                _append(directory, {"id": row["id"], "second": label})
                done += 1
                break
            out("  not a class; a number from the list, a class id, u, m, s or q")
    return done


def run_adjudicate(rows: list[dict[str, Any]], *, directory: Path | None = None, seed: int | None = None,
                   ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> int:
    """Settle each disagreement. The two labels are shown as A and B in random order."""
    tax = taxonomy()
    choices = _choices(tax)
    reviews = load_reviews(directory)
    queue = disputes(rows, reviews)
    rng = random.Random(seed)
    out(f"{len(queue)} disagreements. a / b picks that label; a number or class id picks another; "
        "m = class list, s = skip, q = quit.")
    done = 0
    for k, row in enumerate(queue, 1):
        pair = [row["label"], reviews[row["id"]]["second"]]
        rng.shuffle(pair)
        out(f"\n[{k}/{len(queue)}]")
        _show(row, out)
        out(f"  A: {pair[0]}    B: {pair[1]}")
        while True:
            ans = ask("  final> ").strip().lower()
            if ans == "q":
                return done
            if ans == "m":
                _menu(tax, out)
                continue
            if ans == "s":
                break
            label = {"a": pair[0], "b": pair[1]}.get(ans) or _parse(ans, choices)
            if label:
                _append(directory, {"id": row["id"], "final": label})
                done += 1
                break
            out("  a, b, a number, a class id, m, s or q")
    return done
