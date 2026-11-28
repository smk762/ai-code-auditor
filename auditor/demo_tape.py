"""The hosted demo's tape (#199): one recorded run, from findings to validated fixes, as one JSON file.

auditor.dragonhound.dev replays ``demo/tape/tape.json``; ``scripts/record_demo_tape.py`` does the I/O and
``demo/build.py`` renders it. This module is the pure part, and its rules are the demo's promises:

- Every pack ``demo/run.yaml`` lists is on the tape, whatever its outcome. A wontfix stays in with its note, a
  waiver with its text. Nothing is dropped or reworded to improve the numbers, and the counts are computed from
  the packs, never typed in.
- A resolved pack names the commit that fixed it, and that commit's diff is on the tape.
- Nothing private: paths are made relative to the repo, and every string on the tape goes through the public
  wiki's opsec gate (``wiki.public.gate``: private and link-local addresses, home and mount paths, emails, LAN
  hostnames, private repo names, the deny-list). One hit refuses the whole tape; nothing is redacted.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

FORMAT = "ai-code-auditor-tape"
VERSION = 1

#: How a pack ended, in the order the site lists them.
OUTCOMES = ("repro", "test_only", "waiver", "wontfix")
OUTCOME_LABELS = {
    "repro": "Proven: fails on dev, passes fixed",
    "test_only": "Test-only change, tests ran",
    "waiver": "Resolved on a recorded waiver",
    "wontfix": "Wrong finding, wontfix",
}
#: A diff longer than this is cut, and the tape says by how much.
DIFF_MAX_LINES = 400


class TapeError(ValueError):
    """The run can't become a tape without breaking one of the rules above."""


def relative(text: str, repo: str) -> str:
    """``text`` with every absolute path into a checkout of ``repo`` made relative to the repo."""
    return re.sub(rf"(?<![\w.-])/[^\s`'\"():]*/{re.escape(repo)}/", "", text or "")


def outcome(pack: dict[str, Any]) -> str:
    """repro / test_only / waiver / wontfix, from the pack's status and the evidence it resolved on."""
    status = pack.get("status")
    if status == "wontfix":
        return "wontfix"
    if status != "resolved":
        raise TapeError(f"{pack.get('work_item_id')}: status {status!r}; the tape replays finished packs only")
    kind = ((pack.get("verification") or {}).get("evidence") or {}).get("kind")
    if kind not in ("repro", "test_only", "waiver"):
        raise TapeError(f"{pack.get('work_item_id')}: resolved on {kind!r} evidence, which the demo can't show")
    return kind


def _tests(repro: dict[str, Any]) -> list[dict[str, str]]:
    """Each test the validation ran, with its result on dev and patched; the repro tests first."""
    base, patched = repro.get("base") or {}, repro.get("patched") or {}
    proved = set(repro.get("repro") or [])
    ids = sorted(set(base) | set(patched), key=lambda t: (t not in proved, t))
    return [{"id": t, "dev": base.get(t, "not run"), "patched": patched.get(t, "not run"), "repro": t in proved}
            for t in ids]


def pack_entry(pack: dict[str, Any], repo: str, commit: dict[str, Any] | None) -> dict[str, Any]:
    """One pack as the site shows it."""
    pid = pack["work_item_id"]
    how = outcome(pack)
    if how != "wontfix" and not commit:
        raise TapeError(f"{pid}: resolved, but run.yaml names no commit for it")
    narrative, loc = pack.get("narrative") or {}, pack.get("location") or {}
    evidence = (pack.get("verification") or {}).get("evidence") or {}
    rel = lambda s: relative(str(s or ""), repo)
    entry: dict[str, Any] = {
        "id": pid,
        "title": narrative.get("title", ""),
        "finding_type": narrative.get("finding_type", ""),
        "severity": (pack.get("priority") or {}).get("severity", ""),
        "file": rel(loc.get("file_path")),
        "line": loc.get("line"),
        "snippet": rel(loc.get("code_snippet")),
        "description": rel(narrative.get("description")),
        "recommendation": rel(narrative.get("recommendation")),
        "acceptance_criteria": [rel(c) for c in narrative.get("acceptance_criteria") or []],
        "outcome": how,
        "history": [{"status": h.get("status", ""), "at": h.get("at", ""), "note": rel(h.get("note"))}
                    for h in pack.get("status_history") or []],
        "note": rel(pack.get("status_note")) if how == "wontfix" else "",
        "evidence": {},
        "commit": commit,
    }
    if how != "wontfix":
        entry["evidence"] = {
            "reason": rel(evidence.get("reason")),
            "base_sha": evidence.get("base_sha", ""),
            "at": evidence.get("at", ""),
            "waiver": rel(evidence.get("waiver")),
            "tests": _tests(evidence.get("repro") or {}) if how == "repro" else [],
        }
    return entry


def commit_entry(sha: str, subject: str, diff: str, repo_url: str) -> dict[str, Any]:
    """A fix commit, its diff cut at ``DIFF_MAX_LINES``."""
    lines = diff.splitlines()
    return {"sha": sha, "subject": subject, "url": f"{repo_url}/commit/{sha}",
            "diff": "\n".join(lines[:DIFF_MAX_LINES]), "diff_cut": max(0, len(lines) - DIFF_MAX_LINES)}


def counts(packs: list[dict[str, Any]]) -> dict[str, int]:
    """The run's numbers, from the packs themselves."""
    by = Counter(p["outcome"] for p in packs)
    return {"findings": len(packs), "resolved": len(packs) - by["wontfix"],
            **{k: by[k] for k in OUTCOMES}, "commits": len({p["commit"]["sha"] for p in packs if p["commit"]})}


def build_tape(run: dict[str, Any], packs: list[dict[str, Any]], commits: dict[str, dict[str, Any]],
               *, repo_url: str, recorded_at: str) -> dict[str, Any]:
    """The tape for ``run`` (demo/run.yaml) from its ``packs`` (pack id -> pack) and fix ``commits``
    (short sha from run.yaml -> commit_entry). Raises TapeError when a rule would break."""
    repo = run["repo"]
    by_id = {p["work_item_id"]: p for p in packs}
    wanted = run.get("packs") or {}
    if missing := sorted(set(wanted) - set(by_id)):
        raise TapeError(f"run.yaml lists packs that aren't in reports/agent_packs: {', '.join(missing)}")
    entries = []
    for pid, sha in wanted.items():
        if by_id[pid].get("repo", {}).get("name", repo) != repo:
            raise TapeError(f"{pid} belongs to another repo")
        entries.append(pack_entry(by_id[pid], repo, commits.get(str(sha)) if sha else None))
    entries.sort(key=lambda e: (OUTCOMES.index(e["outcome"]), e["file"], e["title"]))
    pr = run.get("pr") or {}
    return {
        "format": FORMAT,
        "version": VERSION,
        "recorded_at": recorded_at,
        "run": {
            "repo": repo,
            "repo_url": repo_url,
            "run_id": run["run_id"],
            "scanned_sha": run.get("scanned_sha", ""),
            "started_at": run.get("started_at", ""),
            "pr": {**pr, "url": f"{repo_url}/pull/{pr['number']}"} if pr else {},
        },
        "counts": counts(entries),
        "packs": entries,
    }


def strings(value: Any) -> Iterator[str]:
    """Every string in a JSON-like value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def check_public(tape: dict[str, Any], denylist: list[str], private_names: list[str]) -> None:
    """Refuse the tape if any string on it fails the public wiki's opsec gate."""
    from wiki.public import gate

    for text in strings(tape):
        if hit := gate(text, denylist, private_names):
            reason, matched = hit
            raise TapeError(f"opsec gate refused the tape ({reason}: {matched!r}); nothing was written")
