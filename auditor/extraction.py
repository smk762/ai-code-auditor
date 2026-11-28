"""Extraction candidates → a person's decision → one pack per approved candidate.

The ecosystem run proposes candidates: logic duplicated across repos (``patterns.pattern_proposer``,
``reports/extraction_candidates.yaml``). A candidate is a proposal, not work. It becomes a pack
only when someone approves it on ``/ui/architecture`` (or ``ai-audit approve-extraction``) and names
where the shared code goes (``argus-cortex`` by default: it's the library every argus service
already builds on). Dismissing one keeps it out of the list on later runs.

Decisions live in ``rules/extraction_decisions.json`` (``AI_AUDIT_EXTRACTION_DECISIONS_PATH``),
outside git beside the approved rules, because the API writes them.

**Ids.** The proposer numbers candidates by rank, so ``ext-shar-005`` named a different cluster
on another night. ``stabilize_ids`` gives each candidate the id of the earlier candidate or
decision sharing most of its units (``repo:path:symbol``, Jaccard ≥ ``MATCH_MIN``), else a hash
of its units, and records the units per id in ``reports/extraction_ids.json``. An id no candidate
matched is kept there for ``IDS_KEEP_DAYS``, so a cluster one partial run missed gets it back.

**Progress.** A unit is a copy of the logic in one file. Each sync re-reads the selected units
from the mirrors: a copy whose file no longer defines the symbol is ``gone``. The pack lists what
is left. A copy that can't be read is ``unknown``, never ``gone``.

**Packs.** ``sync_packs`` (nightly, after the candidates are written) re-writes the pack of
every approved decision, keeping its status, and closes every ``open`` extraction pack that has
no approval as ``obsolete``. Only ``open`` ones: a claimed or in-progress pack is someone's work.
The work is several PRs, so a passing ``validate_patch`` moves an extraction pack to
``in_progress``, never ``resolved``; the sync resolves it once every selected copy is gone from
the mirrors (``dev``), which is the evidence the migration happened.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from auditor.contracts import utc_now_iso
from auditor.decisions import DecisionConflict as DecisionConflict
from auditor.decisions import close_idle, locked, save_decisions
from auditor.decisions import load_decisions as read_decisions
from auditor.fsutil import atomic_write_text
from auditor.repo_paths import rel_path as _repo_rel_path

logger = logging.getLogger(__name__)

DEFAULT_TARGET = "argus-cortex"
MATCH_MIN = 0.5
IDS_FILE = "extraction_ids.json"
# An id no candidate matched is kept this long, so a cluster missing from one partial run gets its
# old id back when it returns.
IDS_KEEP_DAYS = 90
DECISION_STATUSES = ("approved", "dismissed")
# Statuses the nightly sync resolves once every copy is gone (``unknown`` never counts as gone).
_WORKING = frozenset({"open", "claimed", "in_progress", "unverified"})
# Copies in these places are left out of an approval by default: a test or script helper is a
# poor reason for a service to depend on a library.
_SIDE_PATH = re.compile(r"(^|/)(tests?|scripts|demo|examples?|benchmarks?)/|(^|/)test_[^/]*\.py$|_test\.py$")


def decisions_path() -> str:
    return os.getenv("AI_AUDIT_EXTRACTION_DECISIONS_PATH", "").strip() or "rules/extraction_decisions.json"


# ── units ────────────────────────────────────────────────────────────────────

def rel_path(repo: str, path: str) -> str:
    """A unit path relative to its repo (candidates carry absolute mirror paths)."""
    # The first ``/{repo}/`` is the checkout root: a package named after the repo must survive.
    return _repo_rel_path(repo, str(path).replace("\\", "/"))


def units_of(candidate: dict[str, Any]) -> list[dict[str, str]]:
    """``[{repo, path, symbol}]``, one per (file, symbol), paths relative to the repo."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for u in candidate.get("source_units") or []:
        if not isinstance(u, dict) or not u.get("repo"):
            continue
        syms = u.get("symbols") or []
        for sym in (syms if isinstance(syms, list) else [syms]):
            unit = {"repo": str(u["repo"]), "path": rel_path(str(u["repo"]), str(u.get("path") or "")),
                    "symbol": str(sym)}
            if unit_key(unit) not in seen:
                seen.add(unit_key(unit))
                out.append(unit)
    return out


def unit_key(unit: dict[str, Any]) -> str:
    return f"{unit.get('repo')}:{unit.get('path')}:{unit.get('symbol')}"


def suggested(unit: dict[str, Any]) -> bool:
    """Whether the approval form ticks this copy by default (not a test, script or demo)."""
    return not _SIDE_PATH.search(str(unit.get("path") or ""))


def load_decisions(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """``{candidate_id: decision}``; see ``auditor.decisions``."""
    return read_decisions(path or decisions_path())


# ── stable ids ───────────────────────────────────────────────────────────────

def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def stabilize_ids(candidates: list[dict[str, Any]], output_dir: str | Path,
                  decisions: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Give each candidate a stable id (in place) and record the units per id. Returns *candidates*."""
    ids_path = Path(output_dir) / IDS_FILE
    known: dict[str, set[str]] = {}
    seen: dict[str, str] = {}
    try:
        raw = json.loads(ids_path.read_text(encoding="utf-8"))
        known = {k: set(v) for k, v in (raw.get("ids") or {}).items() if isinstance(v, list)}
        seen = {k: str(v) for k, v in (raw.get("last_seen") or {}).items() if k in known}
    except (OSError, ValueError, AttributeError):
        pass
    for cid, d in (decisions or {}).items():  # a decision's units win: they're what was approved
        keys = {unit_key(u) for u in d.get("all_units") or d.get("units") or [] if isinstance(u, dict)}
        if keys:
            known[cid] = keys

    mine = [{unit_key(u) for u in units_of(c)} for c in candidates]
    pairs = sorted(((s, i, k) for i, keys in enumerate(mine) for k, kk in known.items()
                    if (s := _jaccard(keys, kk)) >= MATCH_MIN), key=lambda t: (-t[0], t[1], t[2]))
    taken: dict[int, str] = {}
    used: set[str] = set()
    for _, i, k in pairs:
        if i not in taken and k not in used:
            taken[i], used = k, used | {k}
    record: dict[str, list[str]] = {}
    for i, c in enumerate(candidates):
        cid = taken.get(i)
        if cid is None:
            cid = "ext-" + hashlib.sha1("\n".join(sorted(mine[i])).encode()).hexdigest()[:8]
            n = 2
            while cid in used or cid in record or cid in known:  # the same unit set twice: keep both, distinctly
                cid = f"{cid.rsplit('~', 1)[0]}~{n}"
                n += 1
        used.add(cid)
        c["id"] = cid
        record[cid] = sorted(mine[i])
    now = utc_now_iso()
    last_seen = {cid: now for cid in record}
    cutoff = (datetime.now(UTC) - timedelta(days=IDS_KEEP_DAYS)).isoformat()
    for k, keys in known.items():  # unmatched this run: keep, so a cluster back from a partial run keeps its id
        if k not in record and seen.get(k, now) >= cutoff:
            record[k] = sorted(keys)
            last_seen[k] = seen.get(k, now)
    atomic_write_text(ids_path, json.dumps({"updated_at": now, "ids": record, "last_seen": last_seen},
                                           indent=1) + "\n")
    return candidates


# ── copy risk (#60) ──────────────────────────────────────────────────────────

COPY_RISK_DAYS = 90


def _agent_touches(repo_paths: dict[str, str | Path], prs: list[dict[str, Any]],
                   since_days: int = COPY_RISK_DAYS) -> dict[tuple[str, str], set[str]]:
    """(repo, repo-relative path) -> the agent PR urls whose commits changed it, from one
    ``git log`` per mirror over the last *since_days*. Reads only. A mirror git can't read (a
    timeout, no git binary) is skipped, like one whose log fails."""
    by_sha: dict[str, str] = {}
    for pr in prs or []:
        for sha in [*(pr.get("commit_shas") or []), pr.get("merge_sha") or ""]:
            if sha:
                by_sha[str(sha)] = str(pr.get("url") or f"{pr.get('repo')}#{pr.get('number')}")
    out: dict[tuple[str, str], set[str]] = {}
    for repo, root in repo_paths.items():
        if not (Path(root) / ".git").exists():
            continue
        # -z: paths come back verbatim (no C-quoting of non-ASCII), and the NUL before each sha
        # marks a commit header no path can be mistaken for (a path may start with "@").
        try:
            r = subprocess.run(["git", "-C", str(root), "log", f"--since={since_days}.days", "--name-only",
                                "--format=%x00%H", "--no-renames", "-z"], capture_output=True, encoding="utf-8",
                               errors="surrogateescape", check=False, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.info("copy risk: no git log for %s: %s", repo, exc)
            continue
        if r.returncode != 0:
            logger.info("copy risk: no git log for %s: %s", repo, (r.stderr or "")[:200])
            continue
        for sha, path in _log_files(r.stdout):
            if cur := by_sha.get(sha, ""):
                out.setdefault((repo, path), set()).add(cur)
    return out


def _log_files(stdout: str) -> Iterable[tuple[str, str]]:
    """``(sha, path)`` pairs from ``git log -z --name-only --format=%x00%H``: each commit is a NUL,
    its sha, a NUL, then its paths (the first after a newline), each NUL-terminated."""
    sha, header, first = "", True, False
    for tok in stdout.split("\0"):
        if not tok:
            header = True
        elif header:
            sha, header, first = tok.strip(), False, True
        else:
            path = tok[1:] if first and tok.startswith("\n") else tok
            first = False
            if path:
                yield sha, path


def annotate_copy_risk(candidates: list[dict[str, Any]], repo_paths: dict[str, str | Path],
                       prs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Add ``copy_risk`` to each candidate (in place): how likely an agent is to copy it again.

    ``score`` = 0.4 · copies (8 or more is 1) + 0.3 · the cluster's 90-day churn score + 0.3 ·
    agent PRs that changed a copy's file in 90 days (3 or more is 1). Copies are what an agent
    finds to imitate, so test, script and demo copies don't count; churn and agent edits say agents
    are working right there."""
    touches = _agent_touches(repo_paths, prs or []) if prs else {}
    for c in candidates:
        units = [u for u in units_of(c) if suggested(u)]  # a test or script helper is rarely copied into code
        files = {(u["repo"], u["path"]) for u in units}
        agent_prs = set().union(*(touches.get(f, set()) for f in files)) if files else set()
        churn = float((c.get("score_breakdown") or {}).get("churn_score") or 0.0)
        score = 0.4 * min(1.0, len(files) / 8) + 0.3 * min(1.0, churn) + 0.3 * min(1.0, len(agent_prs) / 3)
        c["copy_risk"] = {"score": round(score, 3), "copies": len(files), "churn": round(churn, 3),
                          "agent_prs": len(agent_prs)}
    return candidates


# ── progress ─────────────────────────────────────────────────────────────────

def _defines(path: Path, symbol: str) -> bool | None:
    """Whether *path* defines *symbol* (function or class, any depth); None if it can't be read."""
    if not path.exists():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == symbol
               for n in ast.walk(tree))


def progress(decision: dict[str, Any], repo_paths: dict[str, str | Path]) -> list[dict[str, Any]]:
    """Each selected copy with ``state``: ``left`` (still defined), ``gone`` or ``unknown``."""
    rows = []
    for u in decision.get("units") or []:
        root = repo_paths.get(str(u.get("repo")))
        state = "unknown"
        if root and u.get("path"):
            found = _defines(Path(root) / str(u["path"]), str(u.get("symbol")))
            state = "unknown" if found is None else "left" if found else "gone"
        rows.append({**u, "state": state})
    return rows


# ── packs ────────────────────────────────────────────────────────────────────

def pack_id(candidate_id: str) -> str:
    return candidate_id if candidate_id.startswith("pack-") else f"pack-{candidate_id}"


def build_pack(cid: str, decision: dict[str, Any], copies: list[dict[str, Any]], *,
               public_base_url: str = "", run_id: str = ""):
    from auditor.agent_pack import AgentCodingPack

    target = str(decision.get("target_repo") or DEFAULT_TARGET)
    module = str(decision.get("target_module") or "")
    symbol = str(decision.get("symbol") or "")
    left = [c for c in copies if c["state"] != "gone"]
    consumers = sorted({c["repo"] for c in copies if c["repo"] != target})
    where = f"`{module}`" if module else f"a module in {target} you choose"
    copy_lines = [f"- [{c['state']}] {c['repo']}/{c['path']} :: {c['symbol']}" for c in copies]
    desc = [
        str(decision.get("title") or f"Extract {symbol}"),
        "",
        (f"Approved {str(decision.get('decided_at', ''))[:10]}: move this duplicated logic into {where} "
         "and replace each copy below with an import from it."),
        *([f"Note from the approver: {decision['note']}"] if decision.get("note") else []),
        "",
        f"Copies ({len(left)} of {len(copies)} left; `gone` means the file no longer defines the symbol):",
        *copy_lines,
        "",
        ("The candidate came from a similarity scan: the copies are similar, not identical. Read them all "
         "first and design one function that covers what the callers need; where one copy differs on "
         "purpose, leave it and say why in the pack note."),
        "",
        "Order of work:",
        (f"1. {target}: add the shared function with tests, on a branch from origin/dev. Validate with "
         f"POST /audit/validate_patch (repo {target}, pack_id {pack_id(cid)}) and open the PR."),
        ("2. Once that is on dev, one PR per consumer repo: import the shared function, delete the local copy, "
         f"and add or bump the {target} dependency. Validate each the same way."),
        ("3. Set the pack in_progress while PRs are open, and resolved once no copy is left "
         "(the nightly sync re-reads the copies and updates this list)."),
    ]
    if any(r for r in consumers if not r.startswith("argus-")) and target == DEFAULT_TARGET:
        desc += ["", "Some consumers are not argus services (" + ", ".join(r for r in consumers if not r.startswith("argus-"))
                 + "): check they can take an argus-cortex dependency before migrating them; leave them out if not."]
    base = public_base_url.rstrip("/")
    pid = pack_id(cid)
    # The pack's repo is the target, so its location may only name a file there (a copy already in
    # the target); a consumer's copy would point at a path the target doesn't have.
    first = next((c for c in left if c["repo"] == target), None) or {}
    score = decision.get("score")
    return AgentCodingPack(
        work_item_id=pid,
        kind="extraction",
        priority={"severity": "MEDIUM", "score": float(0.6 if score is None else score)},
        repo={"name": target, "path": "", "base_branch": "dev"},
        location={"file_path": str(first.get("path") or ""), "line": 0, "symbol": symbol,
                  "code_snippet": "", "evidence": "\n".join(copy_lines)[:2000]},
        narrative={
            "title": str(decision.get("title") or f"Extract {symbol}"),
            "description": "\n".join(desc),
            "recommendation": f"Add the shared function to {where}, then migrate each consumer in its own PR.",
            "acceptance_criteria": [
                f"{target} exposes the shared function, with tests",
                f"No copy left in: {', '.join(consumers) or '(none)'}",
                "Each consumer imports it from " + target,
            ],
            "missing_evidence": "",
        },
        impact={"impacted_repos": sorted({target, *consumers}), "call_path": [],
                "directly_changed": sorted({f"{c['repo']}/{c['path']}" for c in copies})},
        governance={
            "architecture_rules_hit": [], "related_findings": [], "extraction_candidate_id": cid,
            "extraction": {"target_repo": target, "target_module": module, "copies": copies,
                           "left": len(left), "decided_at": decision.get("decided_at", ""),
                           "note": decision.get("note", "")},
        },
        verification={"suggested_test_commands": ["pytest -q --tb=short"], "tool": "pytest",
                      "max_iterations": 3, "validate_endpoint": "POST /audit/validate_patch"},
        repair={"pack_url": f"{base}/audit/packs/{pid}" if base else "", "slug": f"repair/{cid}"},
        provenance={"source": "extraction_decision", "run_id": run_id, "detected_at": utc_now_iso(),
                    "handoff_path": decisions_path()},
    )


def _write_approved(output_dir: str | Path, cid: str, decision: dict[str, Any],
                    repo_paths: dict[str, str | Path], public_base_url: str, run_id: str = "") -> dict[str, Any]:
    """Write (or refresh) the pack of an approved decision, reopening one closed as obsolete or wontfix.
    The caller holds ``pack_lock``."""
    from auditor.agent_pack import load_pack, write_pack

    copies = progress(decision, repo_paths)
    pack = build_pack(cid, decision, copies, public_base_url=public_base_url, run_id=run_id)
    prev = load_pack(output_dir, pack.work_item_id) or {}
    write_pack(pack, output_dir)  # carries the status over
    if str(prev.get("status") or "open") in ("obsolete", "wontfix"):
        pack.apply_status("open", "extraction approved")
        write_pack(pack, output_dir, preserve_status=False)
    return {"pack_id": pack.work_item_id, "status": pack.status, "left": sum(c["state"] != "gone" for c in copies),
            "copies": len(copies), "done": bool(copies) and all(c["state"] == "gone" for c in copies)}


# ── decisions ────────────────────────────────────────────────────────────────

def _candidates(output_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(output_dir) / "extraction_candidates.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    found = data.get("candidates") if isinstance(data, dict) else data
    return [c for c in found if isinstance(c, dict)] if isinstance(found, list) else []


def decide(output_dir: str | Path, candidate_id: str, status: str, *, target_repo: str = DEFAULT_TARGET,
           target_module: str = "", units: Iterable[str] | None = None, note: str = "",
           repo_paths: dict[str, str | Path] | None = None, public_base_url: str = "",
           path: str | Path | None = None) -> dict[str, Any] | None:
    """Approve or dismiss a candidate. *units* are ``repo:path:symbol`` keys to include (default:
    the suggested ones). Returns None when the candidate isn't in the latest report or decisions.
    Raises ValueError on a bad request and DecisionConflict when its pack is someone's work."""
    from auditor.agent_pack import pack_lock, reindex_pack

    if status not in DECISION_STATUSES:
        raise ValueError(f"status must be one of {DECISION_STATUSES}")
    p = Path(path or decisions_path())
    with locked(p), pack_lock(output_dir):
        decisions = load_decisions(p)
        cand = next((c for c in _candidates(output_dir) if c.get("id") == candidate_id), None)
        prev = decisions.get(candidate_id)
        if cand is None and prev is None:
            return None
        all_units = units_of(cand) if cand else list(prev.get("all_units") or prev.get("units") or [])
        by_key = {unit_key(u): u for u in all_units}
        if units is None:
            chosen = [u for u in all_units if suggested(u)] or all_units
        else:
            keys = list(dict.fromkeys(units))
            unknown = [k for k in keys if k not in by_key]
            if unknown:
                raise ValueError(f"not units of {candidate_id}: {unknown[:5]}")
            chosen = [by_key[k] for k in keys]
        if status == "approved":
            if len({u["repo"] for u in chosen if u["repo"] != target_repo}) < 1:
                raise ValueError("pick at least one copy outside the target repo")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", target_repo or ""):
                raise ValueError("target_repo must be a repo name")
            if target_module and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,150}", target_module):
                raise ValueError("target_module must be a dotted Python module path")
        sym = (cand or prev or {}).get("title", "")
        decision = {
            "status": status, "title": sym, "symbol": chosen[0]["symbol"] if chosen else "",
            "target_repo": target_repo if status == "approved" else "", "target_module": target_module,
            "units": chosen if status == "approved" else [], "all_units": all_units,
            "repos": sorted({u["repo"] for u in all_units}),
            "score": (cand or prev or {}).get("extraction_score", (prev or {}).get("score")),
            "note": (note or "")[:2000], "decided_at": utc_now_iso(),
        }
        pid = pack_id(candidate_id)
        if status == "approved":
            out = _write_approved(output_dir, candidate_id, decision, repo_paths or {}, public_base_url)
        else:
            had = close_idle(output_dir, pid, "wontfix", f"extraction dismissed{': ' + note if note else ''}")
            out = {"pack_id": pid if had else None, "status": "wontfix" if had else None}
        decisions[candidate_id] = decision
        save_decisions(p, decisions)
        reindex_pack(output_dir, pack_id(candidate_id), public_base_url=public_base_url)
    return {"id": candidate_id, "decision": status, **out}


def withdraw(output_dir: str | Path, candidate_id: str, *, public_base_url: str = "",
             path: str | Path | None = None) -> dict[str, Any] | None:
    """Undo a decision: the candidate is a plain proposal again and its pack (if idle) is obsolete."""
    from auditor.agent_pack import pack_lock, reindex_pack

    p = Path(path or decisions_path())
    with locked(p), pack_lock(output_dir):
        decisions = load_decisions(p)
        if candidate_id not in decisions:
            return None
        had = close_idle(output_dir, pack_id(candidate_id), "obsolete", "extraction approval withdrawn")
        del decisions[candidate_id]
        save_decisions(p, decisions)
        reindex_pack(output_dir, pack_id(candidate_id), public_base_url=public_base_url)
    return {"id": candidate_id, "decision": None, "pack_status": "obsolete" if had else None}


def sync_packs(output_dir: str | Path, repo_paths: dict[str, str | Path], *, public_base_url: str = "",
               run_id: str = "", path: str | Path | None = None) -> dict[str, Any]:
    """Nightly: refresh every approved pack's copies and close the open extraction packs nobody approved."""
    from auditor.agent_pack import (
        AgentCodingPack,
        list_packs,
        load_pack,
        pack_lock,
        rebuild_index,
        write_pack,
    )

    p = Path(path or decisions_path())
    out = {"refreshed": 0, "closed": 0, "resolved": 0, "left": {}}
    with pack_lock(output_dir):
        # Read under the lock ``decide`` writes under: an approval landing while this waited is seen,
        # so its fresh pack isn't closed as unapproved.
        decisions = load_decisions(p)
        approved = {cid: d for cid, d in decisions.items() if d.get("status") == "approved"}
        wanted = {pack_id(c) for c in approved}
        for cid, d in approved.items():
            res = _write_approved(output_dir, cid, d, repo_paths, public_base_url, run_id)
            out["refreshed"] += 1
            out["left"][res["pack_id"]] = res["left"]
            if res["done"] and res["status"] in _WORKING:
                data = load_pack(output_dir, res["pack_id"])
                pack = AgentCodingPack.from_dict(data or {}, fallback_id=res["pack_id"])
                pack.apply_status("resolved", f"every copy is gone from dev ({res['copies']} checked)")
                write_pack(pack, output_dir, preserve_status=False)
                out["resolved"] += 1
        for entry in list_packs(output_dir, kind="extraction", status="open", limit=1_000_000):
            pid = str(entry.get("id") or "")
            if pid in wanted:
                continue
            data = load_pack(output_dir, pid)
            if not data or (data.get("status") or "open") != "open":
                continue
            pack = AgentCodingPack.from_dict(data, fallback_id=pid)
            pack.apply_status("obsolete", "not approved: extraction packs exist only for candidates approved "
                                          "on /ui/architecture")
            write_pack(pack, output_dir, preserve_status=False)
            out["closed"] += 1
        rebuild_index(output_dir, public_base_url=public_base_url)
    return out
