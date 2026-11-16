"""Commit trailers for agent fixes (#73): machine-readable in git, readable in the PR.

An agent fix commit ends with one trailer block, the same convention as ``Co-Authored-By:``.
Trailers survive squash merges, show in ``git log``, and parse with ``git interpret-trailers``::

    Pack: pack-argus-lens-0a9ba5beb5
    Pack: pack-argus-lens-77c1d2e3f4
    Evidence: repro
    Class: path_traversal

* ``Pack`` (one line per pack the commit fixes): the key one. A revert of the commit then maps to
  exactly these packs.
* ``Evidence``: the kind ``validate_patch`` reported (repro, tests, test_only, non_code, waiver).
* ``Class``: the packs' finding class (#53), when they have one.
* ``Waiver``: the reason, when the fix went through on an ``evidence_waiver``.

``Evidence`` and ``Class`` describe the commit as its author saw it. The pack store stays the
truth: the PR summary looks every pack up, so it shows the current status, class and evidence.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

KEYS = ("Pack", "Evidence", "Class", "Waiver")
EVIDENCE_KINDS = ("repro", "tests", "test_only", "non_code", "waiver")
_TRAILER_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)\s*:\s*(.*)$")
_PACK_RE = re.compile(r"^pack-[a-z0-9][a-z0-9-]*$")


def parse(message: str) -> dict[str, list[str]]:
    """Trailers of a commit message, keyed by canonical name (``pack`` → ``Pack``).

    Like git, only the last paragraph counts, and only when every line in it is a trailer or an
    indented continuation of one. That way a body line such as "Note: see below" in the middle of
    the message is never read as a trailer."""
    paras = [p for p in re.split(r"\n\s*\n", (message or "").strip()) if p.strip()]
    if len(paras) < 2:  # a subject alone has no trailers
        return {}
    out: dict[str, list[str]] = {}
    last_key = ""
    lines = paras[-1].splitlines()
    parsed: list[tuple[str, str]] = []
    for line in lines:
        if line[:1] in (" ", "\t") and parsed:
            k, v = parsed[-1]
            parsed[-1] = (k, f"{v} {line.strip()}")
            continue
        m = _TRAILER_RE.match(line)
        if not m:
            return {}
        parsed.append((m.group(1), m.group(2).strip()))
    canon = {k.lower(): k for k in KEYS}
    for k, v in parsed:
        last_key = canon.get(k.lower(), k)
        out.setdefault(last_key, []).append(v)
    return out


def pack_ids(message: str) -> list[str]:
    """``Pack:`` trailer values that look like pack ids, in order, de-duplicated."""
    return list(dict.fromkeys(v for v in parse(message).get("Pack", []) if _PACK_RE.match(v)))


def _pack_facts(output_dir: str | Path, pack_id: str) -> dict[str, Any]:
    from auditor.agent_pack import load_pack

    data = load_pack(output_dir, pack_id) or {}
    gov, ver = data.get("governance") or {}, data.get("verification") or {}
    ev = ver.get("evidence") or {}
    return {
        "id": pack_id, "found": bool(data),
        "title": (data.get("narrative") or {}).get("title", ""),
        "repo": (data.get("repo") or {}).get("name", ""),
        "status": data.get("status", ""),
        "class": (gov.get("finding_class") or {}).get("id") or "",
        "class_parent": gov.get("class_parent") or "",
        "evidence": ev.get("kind") or "",
        "repro": list((ev.get("repro") or {}).get("repro") or [])[:3] if isinstance(ev.get("repro"), dict) else [],
        "waiver": ev.get("waiver") or "",
    }


def render(output_dir: str | Path, ids: list[str], *, evidence: str = "", waiver: str = "") -> str:
    """The trailer block for a commit fixing *ids*. *evidence* overrides what the packs record
    (the agent knows it from ``validate_patch`` before the pack is updated)."""
    ids = list(dict.fromkeys(i.strip() for i in ids if i and i.strip()))
    bad = [i for i in ids if not _PACK_RE.match(i)]
    if bad:
        raise ValueError(f"not pack ids: {', '.join(bad)}")
    facts = [_pack_facts(output_dir, i) for i in ids]
    lines = [f"Pack: {i}" for i in ids]
    kinds = list(dict.fromkeys([evidence] if evidence else [f["evidence"] for f in facts if f["evidence"]]))
    if evidence and evidence not in EVIDENCE_KINDS:
        raise ValueError(f"evidence must be one of {', '.join(EVIDENCE_KINDS)}")
    lines += [f"Evidence: {k}" for k in kinds]
    lines += [f"Class: {c}" for c in dict.fromkeys(f["class"] for f in facts if f["class"])]
    reason = waiver or next((f["waiver"] for f in facts if f["waiver"]), "")
    if reason:
        lines.append(f"Waiver: {' '.join(reason.split())}")
    return "\n".join(lines)


def summary_markdown(output_dir: str | Path, ids: list[str], *, public_base_url: str = "") -> str:
    """The visible PR-body section: one row per pack, looked up in the pack store."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return "### Audit packs\n\nThis PR fixes no audit packs."
    base = public_base_url.rstrip("/")
    rows = ["### Audit packs", "", "| Pack | Class | Evidence | Status |", "|---|---|---|---|"]
    for i in ids:
        f = _pack_facts(output_dir, i)
        name = f"[`{i}`]({base}/ui/packs/{i})" if base else f"`{i}`"
        if not f["found"]:
            rows.append(f"| {name} | | | not found |")
            continue
        ev = f["evidence"] or "none yet"
        if f["repro"]:
            ev += " (" + ", ".join(f"`{t}`" for t in f["repro"]) + ")"
        rows.append(f"| {name} {_cell(f['title'])} | {f['class'] or ''} | {ev} | {f['status']} |")
    rows += ["", "<sub>From the commits' `Pack:` trailers (ai-code-auditor #73).</sub>"]
    return "\n".join(rows)


def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "\\|").split())[:90]


def range_pack_ids(repo_path: str | Path, rev_range: str) -> list[str]:
    """Pack ids from the trailers of every commit in *rev_range* (e.g. ``origin/dev..HEAD``)."""
    r = subprocess.run(["git", "log", "--format=%B%x1e", rev_range], cwd=repo_path, capture_output=True,
                       text=True, timeout=60, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or f"git log {rev_range} failed")
    out: list[str] = []
    for msg in r.stdout.split("\x1e"):
        out += pack_ids(msg)
    return list(dict.fromkeys(out))


def issue_fields(body: str) -> dict[str, str]:
    """Answers from an issue filed with the audit-finding form: GitHub renders each field as
    ``### <label>`` followed by the answer (``_No response_`` when left empty)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"^### (.+?)\s*\n(.*?)(?=^### |\Z)", body or "", re.MULTILINE | re.DOTALL):
        val = m.group(2).strip()
        out[m.group(1).strip()] = "" if val == "_No response_" else val
    return out
