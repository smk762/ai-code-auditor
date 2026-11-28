"""Class packs (#54): one guard task per recurring finding class, not N separate fixes.

"Eliminate a class of mistake instead of reviewing it forever." When a guardable class (#53) has
at least ``MIN_INSTANCES`` open packs, or open packs in ``MIN_REPOS`` repos, one ``class_guard``
pack ``pack-class-<class>`` is emitted. Its deliverable is the guard: a ruff or semgrep rule, or a
shared helper plus a rule that steers code to it. The open instance packs become its children
(``governance.class_parent``).

A guard is per repo. Whoever lands it records it on the class pack (``record_guard``: repo,
commit, proof test, fixtures). The nightly step then proves it and closes children:

1. **Merged:** the commit is on the mirror's ``origin/dev``.
2. **Proven:** the proof test (with its fixtures) passes on ``dev`` and does *not* pass when
   copied onto the tree just before the guard commit. It must show the guard firing on a positive
   fixture and staying quiet on a negative one, and the second run shows the test isn't vacuous.
   It's the evidence bar's repro rule (#58), applied to the guard.
3. **Attributed:** the guard closes only what its own rule covers. A guard recorded with a static
   rule (``rule="ruff:B905,S110,S112"``) is run on the child's file as it was just before the guard
   commit. A hit counts toward the child when the code around it names the finding's identifiers
   (``rule_attributed``): two shared names, one of them distinctive, or one when the hit's text
   also sits in the child's cited snippet. Line numbers alone never attribute: a diff_audit child
   cites the start of its unit (a ``def``, a module docstring), not the bad line.
4. **Gone:** every attributed hit is gone from ``dev``, checked with noqa disabled, so a line the
   guard waived with a ``noqa`` still counts as there. Where the nightly scanner reads the file, a
   scan since the guard merged must also not report the child.

A child that passes all of these closes as ``resolved`` (note ``class_guard (…): rule …``). If an
attributed hit is still on ``dev``, or the scan still reports the child, it's marked
``governance.class_exception`` and stays open as ordinary work. A child the rule never fired on is
``unattributed``: it stays open, untouched, and needs its own fix. So does every child of a guard
with no static rule (a helper plus a test): the test proves the guard, but it can't say which
findings the guard removed. Semantic fixes go through ``validate_patch`` and never count here.

When a class pack has no open children left and at least one guard is proven, it resolves itself.
"""
from __future__ import annotations

import builtins
import io
import json
import keyword
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tokenize import NAME, TokenError, generate_tokens
from typing import Any

from auditor.finding_class import taxonomy

logger = logging.getLogger(__name__)

KIND = "class_guard"
ACTIVE = ("open", "claimed", "in_progress", "unverified")
REPORT_NAME = "class_packs.json"
_SEV_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}

GUARD_PLANS = {
    "ruff": ("Enable a ruff rule that flags this pattern (a built-in rule, a banned-api entry, or a plugin). "
             "Baseline the existing hits (fix them, or mark each with a reasoned per-line noqa), then fail CI "
             "on any new one."),
    "semgrep": ("Write a semgrep rule that matches this pattern and run it in CI (the argus-ci reusable "
                "workflow for argus-* repos, #55). Baseline the existing hits, then fail CI on any new one."),
    "helper": ("Add one shared helper that does this safely (argus-cortex for argus-* repos, #56), migrate the "
               "call sites, and add a rule (semgrep, or ruff banned-api) that flags the unsafe call, so new code "
               "has to use the helper."),
}


def enabled() -> bool:
    return os.getenv("AI_AUDIT_CLASS_PACKS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def class_pack_id(cls: str) -> str:
    return f"pack-class-{cls.replace('_', '-')}"


def child_class(data: dict[str, Any]) -> str:
    return str(((data.get("governance") or {}).get("finding_class") or {}).get("id") or "")


def qualifies(children: list[dict[str, Any]], *, min_instances: int, min_repos: int) -> bool:
    repos = {n for c in children if (n := str((c.get("repo") or {}).get("name") or ""))}  # unattributed: no repo
    return len(children) >= min_instances or len(repos) >= min_repos


# ── building the class pack ──────────────────────────────────────────────────

def _instance_line(c: dict[str, Any]) -> str:
    loc, nar = c.get("location") or {}, c.get("narrative") or {}
    sev = (c.get("priority") or {}).get("severity", "MEDIUM")
    from auditor.ui_runs import _display_path

    repo = str((c.get("repo") or {}).get("name") or "")
    path = _display_path(repo, str(loc.get("file_path") or ""))
    return f"- `{c['work_item_id']}` {sev} {repo}:{path}:{loc.get('line', 0)}: {nar.get('title', '')}"


def _is_code(c: dict[str, Any]) -> bool:
    from auditor.pack_gc import nightly_covers

    return nightly_covers(str((c.get("location") or {}).get("file_path") or ""))


def build(cls: str, children: list[dict[str, Any]], prev: dict[str, Any] | None = None, *,
          public_base_url: str = "") -> Any:
    from auditor.agent_pack import AgentCodingPack

    c = taxonomy().get(cls)
    assert c is not None and c.guard in GUARD_PLANS, cls
    pid = class_pack_id(cls)
    repos = Counter(str((x.get("repo") or {}).get("name") or "") for x in children)
    # Code findings first: a docs page is a poor example of the pattern a guard has to match.
    ranked = sorted(children, key=lambda x: (not _is_code(x), -_SEV_ORDER.get(str((x.get("priority") or {}).get("severity", "")).upper(), 2),
                                             -float((x.get("priority") or {}).get("score") or 0), x["work_item_id"]))
    # The pack's severity is its worst child's, code or not; the code-first order is only for examples.
    worst = max(children, key=lambda x: _SEV_ORDER.get(str((x.get("priority") or {}).get("severity", "")).upper(), 2),
                default=None)
    top_sev = str((worst.get("priority") or {}).get("severity") or "MEDIUM") if worst else "MEDIUM"
    prev_gov = (prev or {}).get("governance") or {}
    guards = dict(prev_gov.get("class_guard") or {})
    base = public_base_url.rstrip("/")
    title = f"Eliminate {cls}: one {c.guard} guard for {len(children)} open findings in {len(repos)} repos"
    desc = "\n".join([
        f"**Class:** `{cls}`: {c.description}",
        f"**Guard type:** {c.guard}" + (f" (CWE-{c.cwe})" if c.cwe else ""),
        "",
        "**Proposed guard.** " + GUARD_PLANS[c.guard],
        "",
        "**Open instances by repo:** " + ", ".join(f"{r} {n}" for r, n in repos.most_common()),
        "",
        "**Representative instances:**",
        *[_instance_line(x) for x in ranked[:5]],
        "",
        ("**Rollout:** one repo at a time, starting with the repo with the most instances. In each repo: land the "
         "guard with the existing hits baselined, then ratchet (no new hits, and the baseline only shrinks)."),
        "",
        ("**Recording a guard.** After it merges to `dev` in a repo, record it so the nightly run can prove it "
         f"and close that repo's children: `POST {base}/audit/packs/{pid}/guard` "
         '`{"repo", "commit", "proof_test", "fixtures": [...]}` (mutating auth), or '
         f"`ai-audit packs --guard --id {pid} --repo <repo> --commit <sha> --proof-test <path> --fixture <path>`."),
        "",
        ("The proof test must assert that the guard fires on the positive fixture and is silent on the negative "
         "one. It has to pass on `dev` and must not pass on the tree just before the guard commit."),
    ])
    criteria = [
        "Guard merged to dev in each repo it covers, with the existing hits baselined",
        "Proof test + positive/negative fixtures merged with it, and recorded on this pack",
        "Instances the guard can't express are left open and marked as exceptions",
    ]
    return AgentCodingPack(
        work_item_id=pid, kind=KIND,
        priority={"severity": top_sev, "score": min(1.0, 0.5 + 0.05 * len(children))},
        repo={"name": repos.most_common(1)[0][0] if repos else "ecosystem", "path": "", "base_branch": "dev"},
        location={"file_path": "", "line": 0, "symbol": cls, "code_snippet": "",
                  "evidence": json.dumps([x["work_item_id"] for x in ranked[:20]])},
        narrative={"title": title, "description": desc,
                   "recommendation": GUARD_PLANS[c.guard], "acceptance_criteria": criteria,
                   "finding_type": "class_guard"},
        impact={"directly_changed": [], "transitively_affected": [], "call_path": [],
                "impacted_repos": [r for r, _ in repos.most_common()]},
        governance={"architecture_rules_hit": [], "related_findings": [],
                    "class_id": cls, "guard_type": c.guard,
                    "children": sorted(x["work_item_id"] for x in children),
                    "children_by_repo": dict(repos.most_common()),
                    "class_guard": guards},
        verification={"validate_endpoint": "/audit/packs/{id}/guard".replace("{id}", pid)},
        provenance={"source": "class_packs", "detected_at": datetime.now(UTC).isoformat()},
    )


def emit(output_dir: str | Path, *, min_instances: int | None = None, min_repos: int | None = None,
         public_base_url: str = "") -> dict[str, Any]:
    """Create or refresh class packs and link (or unlink) their children. Idempotent."""
    from auditor.agent_pack import (
        AgentCodingPack,
        _read_pack_yaml,
        packs_dir,
        write_pack,
    )

    out = Path(output_dir)
    min_instances = min_instances if min_instances is not None else _int_env("AI_AUDIT_CLASS_PACK_MIN", 5)
    min_repos = min_repos if min_repos is not None else _int_env("AI_AUDIT_CLASS_PACK_MIN_REPOS", 2)
    tax = taxonomy()
    packs = [d for p in sorted(packs_dir(out).glob("*.yaml")) if (d := _read_pack_yaml(p)) and d.get("work_item_id")]
    by_id = {d["work_item_id"]: d for d in packs}
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in packs:
        cls = child_class(d)
        c = tax.get(cls) if cls else None
        if d.get("kind") != KIND and c and c.guard in GUARD_PLANS and d.get("status", "open") in ACTIVE:
            groups.setdefault(cls, []).append(d)
    emitted, linked, unlinked = [], 0, 0
    parents: dict[str, str] = {}
    for cls, children in sorted(groups.items()):
        pid = class_pack_id(cls)
        if not (qualifies(children, min_instances=min_instances, min_repos=min_repos) or pid in by_id):
            continue
        pack = build(cls, children, by_id.get(pid), public_base_url=public_base_url)
        prev = by_id.get(pid)
        if prev is None or _material(prev) != _material(pack.to_dict()):
            write_pack(pack, out, preserve_status=True)
        emitted.append(pid)
        for ch in children:
            parents[ch["work_item_id"]] = pid
    for d in packs:
        if d.get("kind") == KIND:
            continue
        want = parents.get(d["work_item_id"], "")
        have = str((d.get("governance") or {}).get("class_parent") or "")
        # A closed child keeps its link (history); an active one follows its current class.
        if want == have or (not want and d.get("status", "open") not in ACTIVE):
            continue
        p = AgentCodingPack.from_dict(d)
        if want:
            p.governance["class_parent"] = want
            linked += 1
        else:
            p.governance.pop("class_parent", None)
            unlinked += 1
        write_pack(p, out, preserve_status=False)
    return {"class_packs": emitted, "linked": linked, "unlinked": unlinked}


def _material(d: dict[str, Any]) -> Any:
    """What a re-emit compares, leaving out timestamps so an unchanged pack isn't rewritten."""
    nar, gov = d.get("narrative") or {}, d.get("governance") or {}
    return (nar.get("title"), nar.get("description"), gov.get("children"), (d.get("priority") or {}).get("severity"))


# ── recording and proving guards ─────────────────────────────────────────────

RULE_TOOLS = frozenset({"ruff"})
_RULE_CLOSURE = re.compile(r"class_guard \([^)]*\): rule ")
_RULE_CODE = re.compile(r"^[A-Z]+[0-9]+$")


def parse_rule(spec: str) -> dict[str, Any] | None:
    """``"ruff:B905,S110"`` → ``{"tool": "ruff", "select": ["B905", "S110"]}``; empty → None."""
    spec = (spec or "").strip()
    if not spec:
        return None
    tool, sep, codes = spec.partition(":")
    select = sorted({c.strip().upper() for c in codes.split(",") if c.strip()})
    if not sep or tool.strip().lower() not in RULE_TOOLS or not select or not all(_RULE_CODE.match(c) for c in select):
        raise ValueError("rule must be a tool and its rule codes, like 'ruff:B905,S110,S112'")
    return {"tool": tool.strip().lower(), "select": select}


def record_guard(output_dir: str | Path, pack_id: str, *, repo: str, commit: str, proof_test: str,
                 fixtures: list[str], note: str = "", public_base_url: str = "", rule: str = "") -> dict[str, Any]:
    """Record a merged guard for one repo. Proof and closure happen in the nightly step.
    *rule* (``"ruff:B905,S110"``) is the static rule the guard enforces; without one, no child
    closure can be attributed to the guard."""
    from auditor.agent_pack import AgentCodingPack, load_pack, pack_lock, upsert_index_entries, write_pack

    for name, v in (("repo", repo), ("commit", commit), ("proof_test", proof_test)):
        if not v or not str(v).strip():
            raise ValueError(f"{name} is required")
    # Store what was validated: a padded repo would key a guard no mirror lookup ever finds
    repo, commit, proof_test = str(repo).strip(), str(commit).strip().lower(), str(proof_test).strip()
    fixtures = [str(f).strip() for f in fixtures]
    paths = [proof_test, *fixtures]
    if any(p.startswith("/") or ".." in Path(p).parts for p in paths):
        raise ValueError("proof_test and fixtures must be paths inside the repo")
    # The full sha, not a prefix: the mirror may not have the commit yet, so a mistyped prefix
    # would wait as "pending" forever, indistinguishable from a mirror that hasn't synced.
    if len(commit) != 40 or not all(ch in "0123456789abcdef" for ch in commit.lower()):
        raise ValueError("commit must be the full 40-char hex sha (git rev-parse <commit>)")
    parsed_rule = parse_rule(rule)
    with pack_lock(output_dir):  # load → change → write as one step, so a concurrent status change survives
        data = load_pack(output_dir, pack_id)
        if data is None or data.get("kind") != KIND:
            raise LookupError(f"no class pack {pack_id}")
        pack = AgentCodingPack.from_dict(data, fallback_id=pack_id)
        guards = _record(pack, repo, commit, proof_test, fixtures, note, parsed_rule)
        write_pack(pack, output_dir, preserve_status=False)
        upsert_index_entries(output_dir, [pack], public_base_url=public_base_url)
    return guards[repo]


def _record(pack: Any, repo: str, commit: str, proof_test: str, fixtures: list[str], note: str,
            rule: dict[str, Any] | None = None) -> dict[str, Any]:
    guards = dict(pack.governance.get("class_guard") or {})
    guards[repo] = {"commit": commit.lower(), "proof_test": proof_test, "fixtures": list(fixtures),
                    "note": note[:500], "recorded_at": datetime.now(UTC).isoformat(),
                    "proof": {"status": "pending", "note": "recorded; proven on the next nightly run"}}
    if rule:
        guards[repo]["rule"] = rule
    pack.governance["class_guard"] = guards
    if pack.status == "open":
        pack.apply_status("in_progress", f"guard recorded for {repo} at {commit[:12]}")
    return guards


def _git(mirror: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=mirror, capture_output=True, text=True, timeout=120, check=False)


def _export(mirror: Path, rev: str, dest: Path) -> bool:
    """Extract *rev* of *mirror* into *dest*; False (never an exception) when that fails."""
    try:
        r = subprocess.run(["git", "archive", "--format=tar", rev], cwd=mirror, capture_output=True, timeout=300,
                           check=False)
        if r.returncode != 0:
            return False
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=BytesIO(r.stdout)) as tf:
            tf.extractall(dest, filter="data")
    except (subprocess.TimeoutExpired, OSError, tarfile.TarError) as exc:
        logger.warning("class guard: could not export %s at %s: %s", mirror, rev, exc)
        return False
    return True


ProofRunner = Callable[[Path, Callable[[], Path | None], list[str]], Any]


def _default_runner(output_dir: Path) -> ProofRunner:
    from auditor.evidence import run_repro

    venv = Path(os.getenv("AI_AUDIT_VALIDATE_VENV_DIR") or output_dir / "_validate_venvs")
    node = Path(os.getenv("AI_AUDIT_VALIDATE_NODE_DIR") or output_dir / "_validate_node")
    return lambda tree, base, tests: run_repro(tree, base, tests, venv_cache=venv, node_cache=node)


def prove(rec: dict[str, Any], mirror: Path | None, runner: ProofRunner) -> dict[str, Any]:
    """Merged and proven? Returns the new ``proof`` dict for the guard record."""
    from auditor.pack_gc import Mirror

    now = datetime.now(UTC).isoformat()
    m = Mirror(mirror)
    if not m.ok or m.path is None:
        return {"status": "pending", "note": "no mirror for this repo", "at": now}
    sha = rec["commit"]
    if _git(m.path, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        return {"status": "pending", "note": "the mirror doesn't have this commit yet", "at": now}
    if _git(m.path, "merge-base", "--is-ancestor", sha, m.ref).returncode != 0:
        return {"status": "pending", "note": f"not merged to {m.ref} yet", "at": now}
    dev_sha = _git(m.path, "rev-parse", m.ref).stdout.strip()
    merged_at = _git(m.path, "show", "-s", "--format=%cI", sha).stdout.strip()
    tests = [rec["proof_test"], *rec.get("fixtures", [])]
    with tempfile.TemporaryDirectory(prefix="class-guard-") as tmp:
        tree, before = Path(tmp) / "dev", Path(tmp) / "before"
        if not _export(m.path, dev_sha, tree):
            return {"status": "pending", "note": "could not export dev", "at": now}
        # the record is re-read from a pack file, so check containment again: an absolute or ``..`` path
        # would test (and run) a host file instead of one in the exported dev tree
        root = tree.resolve()
        escaping = [t for t in tests if Path(t).is_absolute() or not (tree / t).resolve().is_relative_to(root)]
        if escaping:
            return {"status": "failed", "note": f"outside the repo: {', '.join(escaping)}", "at": now,
                    "dev_sha": dev_sha}
        missing = [t for t in tests if not (tree / t).is_file()]
        if missing:
            return {"status": "failed", "note": f"not on dev: {', '.join(missing)}", "at": now, "dev_sha": dev_sha}

        def base() -> Path | None:
            return before if _export(m.path, f"{sha}^", before) else None

        res = runner(tree, base, tests)
    patched = getattr(res, "patched", {}) or {}
    repro = list(getattr(res, "repro", []) or [])
    failing = sorted(t for t, s in patched.items() if s != "passed" and s != "skipped")
    proof = {"at": now, "dev_sha": dev_sha, "merged_at": merged_at, "tests": sorted(patched),
             "repro": repro, "notes": list(getattr(res, "notes", []) or [])[:5]}
    if not patched:
        return {**proof, "status": "failed", "note": "the proof test produced no results on dev"}
    if failing:
        return {**proof, "status": "failed", "note": f"failing on dev: {', '.join(failing[:5])}"}
    if not repro:
        return {**proof, "status": "failed",
                "note": "the proof test also passes without the guard, so it doesn't show the guard working"}
    return {**proof, "status": "proven", "note": f"{len(repro)} test(s) pass with the guard and fail without it"}


# ── closing children ─────────────────────────────────────────────────────────

def _ts(s: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RuleHit:
    line: int
    code: str
    text: str  # the source line, as written


RuleRunner = Callable[[str, str, dict[str, Any]], "list[RuleHit] | None"]


def rule_hits(source: str, path: str, rule: dict[str, Any]) -> list[RuleHit] | None:
    """The guard rule's hits in *source* (as file *path*), noqa disabled: a waived line is still the
    pattern. None when the rule can't run (no ruff, a crash), which must never read as "no hits"."""
    exe = shutil.which("ruff") or str(Path(sys.executable).with_name("ruff"))
    if rule.get("tool") != "ruff" or not Path(exe).is_file():
        return None
    try:
        r = subprocess.run([exe, "check", "--isolated", "--no-cache", "--ignore-noqa", "--output-format", "json",
                            "--select", ",".join(rule["select"]), "--stdin-filename", path, "-"],
                           check=False, input=source, capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout or "null")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if r.returncode not in (0, 1) or not isinstance(data, list):
        return None
    # A file ruff couldn't parse was never checked: its diagnostic has no rule code (null, E999, or
    # "invalid-syntax" depending on the version), and dropping it would read as "no hits".
    if any(not isinstance(h, dict) or not _RULE_CODE.match(str(h.get("code") or "")) or h.get("code") == "E999"
           for h in data):
        return None
    lines = source.splitlines()
    out = []
    for h in data:
        row = int((h.get("location") or {}).get("row") or 0)
        if _selected(h["code"], rule["select"]) and 0 < row <= len(lines):
            out.append(RuleHit(row, h["code"], lines[row - 1]))
    return out


def _selected(code: str, select: list[str]) -> bool:
    """Whether ruff's ``--select`` entries cover *code*: like ruff, an entry is a code or a prefix of
    codes ("B9" selects B901 to B905), and "ALL" is every rule."""
    return any(s == "ALL" or code.startswith(s) for s in select)


_STOP_NAMES = frozenset(keyword.kwlist) | frozenset(dir(builtins)) | {"self", "cls"}
_TEXT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _code_names(lines: list[str]) -> set[str]:
    """Identifiers in *lines* of code (comments and strings skipped), lowercased."""
    out: set[str] = set()
    try:
        for tok in generate_tokens(io.StringIO("\n".join(x.strip() for x in lines) + "\n").readline):
            if tok.type == NAME and len(tok.string) >= 3 and tok.string not in _STOP_NAMES:
                out.add(tok.string.lower())
    except (TokenError, SyntaxError):  # a partial statement: keep what was read
        pass
    return out


def _distinctive(name: str) -> bool:
    return "_" in name.strip("_") or any(c.isdigit() for c in name) or len(name) >= 6


def _norm_code(line: str) -> str:
    return " ".join(line.split("#", 1)[0].split())


def rule_attributed(hit: RuleHit, lines: list[str], finding_text: str, snippet: str = "") -> set[str]:
    """The identifiers that tie *hit* to a finding, or an empty set. The hit's line is used; for an
    ``except`` line, which names nothing of its own, so is the ``try`` body up to three lines above
    it. Two shared names with one distinctive attribute it; one distinctive name is enough when the
    hit's code also sits in the finding's cited snippet."""
    window = [lines[hit.line - 1]]
    if _norm_code(window[0]).startswith("except"):
        above = [x for x in lines[max(0, hit.line - 4):hit.line - 1] if x.strip()]
        window = above + window
    text = {t.lower() for t in _TEXT_NAME.findall(finding_text)}
    shared = _code_names(window) & text
    if not any(_distinctive(n) for n in shared):
        return set()
    in_snippet = bool(_norm_code(hit.text)) and _norm_code(hit.text) in {
        _norm_code(x.split("|", 1)[1]) for x in snippet.splitlines() if "|" in x}
    return shared if len(shared) >= 2 or in_snippet else set()


def judge_child(child: Any, proof: dict[str, Any], scans: list[Any], mirror: Any, rec: dict[str, Any] | None = None,
                finding_text: str = "", runner: RuleRunner | None = None) -> tuple[str, str]:
    """``("close" | "exception" | "wait" | "unattributed", reason)`` for one active child in a repo
    with a proven guard. *child* is a ``trust_metrics.PackView``; *scans* are that repo's scans,
    oldest first; *finding_text* is the child's title, description and recommendation."""
    from auditor.pack_gc import _reported, nightly_covers

    rule = (rec or {}).get("rule")
    if not rule:
        return "unattributed", "the guard has no static rule, so no closure can be attributed to it"
    if mirror is None or not mirror.ok or mirror.path is None:
        return "wait", "no mirror to read the code before the guard"
    runner = runner or rule_hits
    before = _git(mirror.path, "show", f"{rec['commit']}^1:{child.path}")
    if before.returncode != 0:
        return "unattributed", "the file wasn't there before the guard"
    pre = runner(before.stdout, child.path, rule)
    if pre is None:
        return "wait", f"couldn't run the guard rule ({rule.get('tool')})"
    lines = before.stdout.splitlines()
    tied = [(h, names) for h in pre if (names := rule_attributed(h, lines, finding_text, child.snippet or ""))]
    if not tied:
        return "unattributed", "the guard's rule didn't fire on this finding's code before the guard"
    now = mirror.lines(child.path) if child.path in mirror.files() else []
    if now is None:
        return "wait", "can't read the file on dev"
    dev = runner("\n".join(now) + "\n", child.path, rule) if now else []
    if dev is None:
        return "wait", f"couldn't run the guard rule ({rule.get('tool')}) on dev"
    on_dev = {(h.code, _norm_code(h.text)) for h in dev}
    where = ", ".join(f"{h.code} at {child.path}:{h.line} ({'/'.join(sorted(n)[:3])})" for h, n in tied[:3])
    still = [h for h, _ in tied if (h.code, _norm_code(h.text)) in on_dev]
    if still:
        return "exception", f"rule {still[0].code} still fires on dev (or is waived by a noqa): {where}"
    merged = _ts(proof.get("merged_at") or "")
    after = [s for s in scans if merged and s.at > merged]
    clause = ""
    if nightly_covers(child.path):
        if not after:
            return "wait", "no nightly scan since the guard merged"
        if _reported(child, after[-1]):
            return "exception", f"still reported by scan {after[-1].run_id} after the guard merged"
        clause = f"; scan {after[-1].run_id} doesn't report it"
    return "close", f"rule {where} fired before the guard and not on dev{clause}"


def run(output_dir: str | Path | None = None, *, session=None, mirrors_path: str | Path = "config/repo_mirrors.yaml",
        runner: ProofRunner | None = None, dry_run: bool = False, public_base_url: str = "") -> dict[str, Any]:
    """Emit class packs, prove recorded guards, close children whose guard holds."""
    from auditor.agent_pack import (
        AgentCodingPack,
        load_pack,
        rebuild_index,
        refresh_pack_rag,
        write_pack,
    )
    from auditor.pack_gc import Mirror, mirror_paths
    from auditor.trust_metrics import load_packs, load_scans

    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    rep: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "dry_run": dry_run}
    if not dry_run:
        rep["emit"] = emit(out, public_base_url=public_base_url)
    mpaths = mirror_paths(mirrors_path)
    runner = runner or _default_runner(out)
    views = {v.id: v for v in load_packs(out)}
    scans_by_repo: dict[str, list[Any]] | None = None
    proofs, closed, exceptions, waiting, unattributed = [], [], [], [], []
    changed = False
    for v in [x for x in views.values() if x.kind == KIND]:
        data = load_pack(out, v.id) or {}
        gov = data.get("governance") or {}
        guards = dict(gov.get("class_guard") or {})
        touched = False
        for repo, rec in guards.items():
            bad = _bad_guard_record(rec)
            if bad:  # one malformed record is reported and skipped, never a crash mid-run
                proofs.append({"pack": v.id, "repo": repo, "status": "invalid", "note": bad})
                continue
            proof = rec.get("proof") or {}
            dev_now = ""
            if mpaths.get(repo) and Mirror(mpaths[repo]).ok:
                mm = Mirror(mpaths[repo])
                dev_now = _git(mm.path, "rev-parse", mm.ref).stdout.strip() if mm.path else ""
            if proof.get("status") != "proven" and not (proof.get("status") == "failed" and proof.get("dev_sha") == dev_now):
                proof = prove(rec, mpaths.get(repo), runner)
                rec["proof"] = proof
                touched = True
                proofs.append({"pack": v.id, "repo": repo, "status": proof["status"], "note": proof.get("note", "")})
            if proof.get("status") != "proven":
                continue
            if scans_by_repo is None:
                scans_by_repo = _scans_by_repo(session, load_scans)
            mirror = Mirror(mpaths.get(repo)) if mpaths.get(repo) else None
            for cid in gov.get("children") or []:
                ch = views.get(cid)
                if ch is None or ch.repo != repo or ch.status not in ACTIVE:
                    continue
                narrative = (load_pack(out, cid) or {}).get("narrative") or {}
                text = " ".join(str(narrative.get(k) or "") for k in ("title", "description", "recommendation"))
                verdict, why = judge_child(ch, proof, scans_by_repo.get(repo, []), mirror, rec, text)
                item = {"pack": cid, "parent": v.id, "repo": repo, "reason": why}
                if verdict in ("wait", "unattributed"):
                    (waiting if verdict == "wait" else unattributed).append(item)
                    continue
                (closed if verdict == "close" else exceptions).append(item)
                if dry_run:
                    continue
                cdata = load_pack(out, cid)
                if not cdata:
                    continue
                cp = AgentCodingPack.from_dict(cdata, fallback_id=cid)
                if verdict == "close":
                    cp.apply_status("resolved", f"class_guard ({v.id}): {why}"[:2000])
                    cp.verification["evidence"] = {"kind": "class_guard", "parent": v.id, "repo": repo,
                                                   "guard_commit": rec.get("commit"), "proof": proof.get("repro", []),
                                                   "rule": rec.get("rule"), "reason": why}
                    write_pack(cp, out, preserve_status=False)
                    refresh_pack_rag(out, cp.to_dict())
                elif (cp.governance.get("class_exception") or {}).get("reason") != why:
                    cp.governance["class_exception"] = {"parent": v.id, "reason": why,
                                                        "at": datetime.now(UTC).isoformat()}
                    write_pack(cp, out, preserve_status=False)
                changed = True
        if dry_run:
            continue
        if touched:
            pack = AgentCodingPack.from_dict(data, fallback_id=v.id)
            pack.governance["class_guard"] = guards
            write_pack(pack, out, preserve_status=False)
            changed = True
        # A class pack with nothing open left, and a guard proven somewhere, is done.
        fresh = load_pack(out, v.id) or {}
        kids = [k for c in (fresh.get("governance") or {}).get("children") or []
                if (k := _child_pack(out, c)) is not None]
        proven_any = any(not _bad_guard_record(g) and (g.get("proof") or {}).get("status") == "proven"
                         for g in ((fresh.get("governance") or {}).get("class_guard") or {}).values())
        if fresh.get("status") in ACTIVE and proven_any and not any(k.get("status", "open") in ACTIVE for k in kids):
            pack = AgentCodingPack.from_dict(fresh, fallback_id=v.id)
            pack.apply_status("resolved", "every child closed and a guard is proven")
            write_pack(pack, out, preserve_status=False)
            changed = True
    if changed:
        rebuild_index(out, public_base_url=public_base_url)
    rep.update({"proofs": proofs, "closed": closed, "exceptions": exceptions, "waiting": len(waiting),
                "unattributed": len(unattributed), "waiting_reasons": dict(Counter(w["reason"] for w in waiting))})
    if not dry_run:
        (out / REPORT_NAME).write_text(json.dumps(rep, indent=1))
    return rep


def _bad_guard_record(rec: Any) -> str:
    """Why a stored guard record can't be proven or close anything, or "" when it is usable."""
    if not isinstance(rec, dict):
        return "the guard record is not a mapping"
    missing = [k for k in ("commit", "proof_test") if not isinstance(rec.get(k), str) or not rec[k].strip()]
    if missing:
        return f"the guard record has no {' or '.join(missing)}"
    if not isinstance(rec.get("proof") or {}, dict):
        return "the guard record's proof is not a mapping"
    return ""


def _child_pack(out: Path, cid: str) -> dict[str, Any] | None:
    """A child pack's data; None only when the child pack is gone (no file). A file that exists but
    can't be read stays in the way as an open child: unread is never closed."""
    from auditor.agent_pack import load_pack, pack_file_path

    data = load_pack(out, cid)
    if data is not None:
        return data
    return {} if pack_file_path(out, cid).exists() else None


def _scans_by_repo(session, load_scans) -> dict[str, list[Any]]:
    if session is None:
        from auditor import ui

        with ui._db_session() as s:
            scans = load_scans(s)
    else:
        scans = load_scans(session)
    out: dict[str, list[Any]] = {}
    for s in scans:
        out.setdefault(s.repo, []).append(s)
    return out


def run_quietly(output_dir: str | Path | None = None, **kw: Any) -> dict[str, Any]:
    """Nightly step: never fails the run."""
    if not enabled():
        return {"skipped": "AI_AUDIT_CLASS_PACKS=0"}
    try:
        from conductor.publish import public_base_url

        r = run(output_dir, public_base_url=public_base_url(), **kw)
        return {"emit": r.get("emit"), "proofs": len(r["proofs"]), "closed": len(r["closed"]),
                "exceptions": len(r["exceptions"]), "waiting": r["waiting"],
                "unattributed": r.get("unattributed", 0)}
    except Exception as exc:  # a housekeeping failure must not fail the audit
        logger.exception("class packs failed")
        return {"error": str(exc)[:300]}


# ── the gate ─────────────────────────────────────────────────────────────────

def lifecycle_metric(output_dir: str | Path) -> dict[str, Any]:
    """For the ``class_packs.lifecycle`` gate: of the children a class guard closed by its own rule,
    the share that stayed closed (never reopened, never recurred as a new pack). A closure counts
    only when it was rule-attributed (note ``class_guard (…): rule …``); an older closure that rested
    on the scan's silence alone doesn't."""
    from auditor.trust_metrics import load_packs

    views = load_packs(output_dir)
    recurred = {v.recurrence_of for v in views if v.recurrence_of}
    closed = held = 0
    for v in views:
        hist = v.history
        idx = [i for i, h in enumerate(hist) if h.get("status") == "resolved"
               and _RULE_CLOSURE.match(str(h.get("note") or ""))]
        if not idx:
            continue
        closed += 1
        later = hist[idx[0] + 1:]
        if v.id not in recurred and not any(h.get("status") in ACTIVE for h in later):
            held += 1
    return {"n": closed, "held": held, "rate": round(held / closed, 4) if closed else None}
