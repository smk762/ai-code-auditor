"""Pack GC: close finding packs whose issue the scanners no longer report, as ``obsolete`` (#67).

Nothing else closes a finding pack except an agent or a person, so packs for issues fixed outside
the pack loop (a batch PR, a refactor, a deleted file) stay open forever and add noise to bundles,
the board and every trust metric. ``obsolete`` means "no longer reported". It is not a fix
(``resolved``) and not a decision (``wontfix``). A pack closed this way reopens when the issue is
found again (``agent_pack._carry_over_lifecycle``), and the trust metrics count those reopenings
as the GC's false-close rate.

Only ``open`` packs are considered by those rules. Claimed, in-progress and unverified packs have
someone on them or a fix in hand, so they are never touched.

``wontfix`` packs are a decision, and the decision is what keeps a re-reported false positive from
reopening, so they are kept unless the decision has nothing left to apply to. Those are closed as
``obsolete`` too, so a later report reopens them for a fresh look instead of staying suppressed:

==========================  ==================================================================
Rule                        Closes a wontfix pack when
==========================  ==================================================================
``keeper_closed``           It is a duplicate, and the pack it duplicates is resolved or obsolete.
``wontfix_file_gone``       (Not a duplicate.) As ``file_gone``.
``wontfix_code_changed``    (Not a duplicate, not ``ask``.) As ``code_changed``: the cited lines
                            are gone, and a scanned file has had a silent nightly scan since.
==========================  ==================================================================

A wontfix pack is never closed for not being rescanned or for age: with its code unchanged, the
detector may report it again, and the decision should still hold.

======================  ======================================================================
Rule                    Closes an open finding pack when
======================  ======================================================================
``file_gone``           Its file is absent from the mirror's ``origin/dev`` (else ``HEAD``).
``not_rescanned``       (Nightly packs.) The last ``misses`` consecutive nightly scans of its repo
                        since it was last seen (``detected_at`` refreshes on every re-emit) don't
                        report it. The reason says whether the file changed in between.
``code_changed``        (Scan kinds.) The cited lines (the pack's line ±2, from its snippet) no longer
                        appear anywhere in the file at ``origin/dev``, so a move isn't a change. And,
                        for a file the nightly scanner covers, no nightly scan since the pack was last
                        seen reports it. For files it doesn't cover (docs, config), the change is
                        enough.
``expired``             (diff_audit packs.) Open for more than ``ttl_days`` since last seen.
======================  ======================================================================

Packs filed by a person (``ask``) are only closed by ``file_gone``. A nightly scan not reporting
a diff_audit finding is never enough on its own, because it is a different detector that may
never have reported it.

Two safety holds apply. A repo whose latest scan reports nothing although the scan before it
didn't is skipped (likely a broken scan). A plan that would close more than half of a repo's open
packs, or of its wontfix packs, and more than ``HOLD_MIN``, is held unless ``force``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from auditor.fsutil import atomic_write_text
from auditor.repo_paths import mirror_paths
from auditor.trust_metrics import PackView, Scan, _same_issue, load_packs, load_scans

logger = logging.getLogger(__name__)

REPORT_NAME = "pack_gc.json"
HOLD_MIN = 10
HOLD_SHARE = 0.5
SCAN_KINDS = frozenset({"nightly"})          # re-detected by the nightly scan itself
TTL_KINDS = frozenset({"diff_audit"})        # one-off reviews of a diff
HUMAN_KINDS = frozenset({"ask"})             # filed by a person from chat: never expire


def enabled() -> bool:
    return os.getenv("AI_AUDIT_PACK_GC", "1").strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass
class Closure:
    pack_id: str
    repo: str
    path: str
    title: str
    kind: str
    rule: str
    reason: str
    from_status: str = "open"


# ── mirror facts ─────────────────────────────────────────────────────────────

class Mirror:
    """Read-only facts about a repo's ``origin/dev`` (else ``HEAD``) in its local mirror."""

    def __init__(self, path: Path | None):
        self.path = path if path and (path / ".git").exists() else None
        self.ref = ""
        self._files: set[str] | None = None
        self._text: dict[str, list[str] | None] = {}
        if self.path:
            for ref in ("origin/dev", "HEAD"):
                if self._git("rev-parse", "--verify", "-q", ref).returncode == 0:
                    self.ref = ref
                    break

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Decode leniently: a Latin-1 or binary blob must not raise out of ``lines()``.
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=60, check=False)

    @property
    def ok(self) -> bool:
        return bool(self.path and self.ref)

    def files(self) -> set[str]:
        if not self.ok:  # never run git in the process CWD or against the index
            return set()
        if self._files is None:
            # -z: without it, paths with non-ASCII bytes, quotes or newlines come back C-quoted
            r = self._git("ls-tree", "-r", "-z", "--name-only", self.ref)
            self._files = set(filter(None, r.stdout.split("\0"))) if r.returncode == 0 else set()
        return self._files

    def lines(self, path: str) -> list[str] | None:
        """The file's lines at the ref, or None when it can't be read."""
        if not self.ok:
            return None
        if path not in self._text:
            r = self._git("show", f"{self.ref}:{path}")
            self._text[path] = r.stdout.splitlines() if r.returncode == 0 else None
        return self._text[path]

    def last_change(self, path: str) -> datetime | None:
        if not self.ok:
            return None
        r = self._git("log", "-1", "--format=%cI", self.ref, "--", path)
        try:
            return datetime.fromisoformat(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
        except ValueError:
            return None


# ── planning ─────────────────────────────────────────────────────────────────

_SNIPPET_LINE_RE = re.compile(r"^\s*(\d+)\| ?(.*)$")
CORE_RADIUS = 2


def _norm(line: str) -> str:
    return " ".join(line.split())


def cited_core(snippet: str, line: int) -> list[str]:
    """The non-blank lines within ±CORE_RADIUS of *line* in a numbered snippet, whitespace-normalised.
    Empty when the snippet has no numbered lines there or they are all trivial (``}``, ``)``…)."""
    core = []
    for raw in snippet.splitlines():
        m = _SNIPPET_LINE_RE.match(raw)
        if m and abs(int(m.group(1)) - line) <= CORE_RADIUS and (t := _norm(m.group(2))):
            core.append(t)
    return core if any(len(t) >= 4 and re.search(r"\w", t) for t in core) else []


def code_still_there(core: list[str], current: list[str]) -> bool:
    """Whether *core* occurs as a contiguous run of non-blank lines anywhere in *current*."""
    cur = [t for t in (_norm(x) for x in current) if t]
    n = len(core)
    return any(cur[i:i + n] == core for i in range(len(cur) - n + 1))


def nightly_covers(path: str) -> bool:
    """Whether the nightly scanner reads this file at all (its silence about others means nothing)."""
    from auditor.repo_scanner import IGNORE_DIRS, SOURCE_EXTENSIONS

    parts = Path(path).parts
    return Path(path).suffix in SOURCE_EXTENSIONS and not any(part in IGNORE_DIRS for part in parts)


def _reported(p: PackView, scan: Scan) -> bool:
    return any(f["path"] == p.path and _same_issue(p.title, p.line, f["title"], f["line"]) for f in scan.findings)


def _broken_latest_scan(seq: list[Scan]) -> bool:
    return len(seq) >= 2 and not seq[-1].findings and bool(seq[-2].findings)


def plan(packs: list[PackView], scans: list[Scan], mirrors: dict[str, Mirror], *, now: datetime,
         misses: int = 3, ttl_days: int = 30, force: bool = False,
         repo: str = "") -> dict[str, Any]:
    """Which open finding packs to close and why, plus what was held back. Changes nothing."""
    scans_by_repo: dict[str, list[Scan]] = defaultdict(list)
    for s in scans:
        scans_by_repo[s.repo].append(s)
    status_of = {p.id: p.status for p in packs}
    considered: dict[tuple[str, str], int] = defaultdict(int)  # (repo, status) -> packs looked at
    closures: list[Closure] = []
    skipped: dict[str, str] = {}
    for p in packs:
        if not p.is_finding or not p.path or (repo and p.repo != repo):
            continue
        if not (p.status == "open" and not p.duplicate_of) and p.status != "wontfix":
            continue
        considered[(p.repo, p.status)] += 1
        seq = scans_by_repo.get(p.repo, [])
        if _broken_latest_scan(seq):
            skipped[p.repo] = f"latest scan {seq[-1].run_id} reports nothing; the one before it didn't"
        m = mirrors.get(p.repo)
        seq = seq if p.repo not in skipped else []
        if p.status == "wontfix":
            c = _judge_wontfix(p, seq, m, keeper_status=status_of.get(p.duplicate_of, ""))
        else:
            c = _judge(p, seq, m, now=now, misses=misses, ttl_days=ttl_days)
        if c:
            closures.append(c)
    held: dict[str, str] = {}
    if not force:
        per_repo: dict[tuple[str, str], int] = defaultdict(int)
        for c in closures:
            per_repo[(c.repo, c.from_status)] += 1
        tripped = {(r, st) for (r, st), n in per_repo.items()
                   if n > HOLD_MIN and n > HOLD_SHARE * considered[(r, st)]}
        # Each status is held on its own count, and every tripped reason is reported.
        for r, st in sorted(tripped):
            msg = f"would close {per_repo[(r, st)]} of {considered[(r, st)]} {st} packs (> {HOLD_SHARE:.0%})"
            held[r] = f"{held[r]}; {msg}" if r in held else msg
        held = {r: f"{msg}; rerun with force" for r, msg in held.items()}
        closures = [c for c in closures if (c.repo, c.from_status) not in tripped]
    return {"closures": closures, "held": held, "skipped": skipped,
            "open_considered": sum(n for (_, st), n in considered.items() if st == "open"),
            "wontfix_considered": sum(n for (_, st), n in considered.items() if st == "wontfix")}


def _code_changed(p: PackView, seq: list[Scan], m: Mirror | None) -> str:
    """Why the pack's cited lines count as changed away, or "" when they don't."""
    seen = p.detected_at
    if not (m is not None and m.ok and seen and (core := cited_core(p.snippet, p.line))):
        return ""
    current = m.lines(p.path)
    if current is None or code_still_there(core, current):
        return ""
    if not nightly_covers(p.path):
        return f"the cited lines around L{p.line} are gone from {m.ref} (a file the nightly scan doesn't read)"
    later = [s for s in seq if s.at > seen and s.covers(p.path)]
    if later and not any(_reported(p, s) for s in later):
        return (f"the cited lines around L{p.line} are gone from {m.ref}, and "
                f"{len(later)} nightly scan(s) since {seen:%Y-%m-%d} don't report it")
    return ""


def _judge_wontfix(p: PackView, seq: list[Scan], m: Mirror | None, *, keeper_status: str) -> Closure | None:
    def close(rule: str, reason: str) -> Closure:
        return Closure(p.id, p.repo, p.path, p.title, p.kind, rule, reason, from_status="wontfix")

    if p.duplicate_of:
        if keeper_status in {"resolved", "obsolete"}:
            return close("keeper_closed", f"duplicate of {p.duplicate_of}, which is {keeper_status}")
        return None
    if m is not None and m.ok and p.path not in m.files():
        return close("wontfix_file_gone", f"{p.path} is not on {m.ref}")
    if p.kind in HUMAN_KINDS:
        return None
    if why := _code_changed(p, seq, m):
        return close("wontfix_code_changed", why)
    return None


def _judge(p: PackView, seq: list[Scan], m: Mirror | None, *, now: datetime, misses: int,
           ttl_days: int) -> Closure | None:
    def close(rule: str, reason: str) -> Closure:
        return Closure(p.id, p.repo, p.path, p.title, p.kind, rule, reason)

    if m is not None and m.ok and p.path not in m.files():
        return close("file_gone", f"{p.path} is not on {m.ref}")
    if p.kind in HUMAN_KINDS:
        return None
    seen = p.detected_at
    # A scan that couldn't review the pack's file says nothing about it (#blind-scans).
    seq = [s for s in seq if s.covers(p.path)]
    later = [s for s in seq if seen and s.at > seen]
    if p.kind in SCAN_KINDS:
        recent = later[-misses:]
        # misses < 1 would make zero scans "enough"; seen None leaves nothing to count from.
        if misses >= 1 and seen and len(recent) >= misses and not any(_reported(p, s) for s in recent):
            changed = m.last_change(p.path) if m is not None and m.ok else None
            if changed is None:  # no usable mirror or history: don't claim either way
                how = "file history unavailable"
            elif changed > seen:
                how = "the file changed since"
            else:
                how = "the file is unchanged (detector drift)"
            return close("not_rescanned", f"not reported by the last {misses} nightly scans since "
                         f"{seen:%Y-%m-%d}; {how}")
        return None
    if why := _code_changed(p, seq, m):
        return close("code_changed", why)
    if p.kind in TTL_KINDS and seen and now - seen > timedelta(days=ttl_days):
        return close("expired", f"open {(now - seen).days} days since last seen (TTL {ttl_days})")
    return None


# ── applying ─────────────────────────────────────────────────────────────────

def apply(output_dir: str | Path, closures: list[Closure], *, public_base_url: str = "") -> int:
    """Set each pack ``obsolete`` with the reason in its history and ``governance.gc``.
    A pack whose status moved off the one it was planned from is left alone."""
    from auditor.agent_pack import (
        AgentCodingPack,
        load_pack,
        pack_lock,
        refresh_pack_rag,
        upsert_index_entries,
        write_pack,
    )

    now = datetime.now(UTC).isoformat()
    closed: list[AgentCodingPack] = []
    # Check and write under the pack lock: an agent's claim landing between "it is open" and the
    # write would otherwise be overwritten with obsolete (CLAUDE.md: GC never closes a claimed pack).
    with pack_lock(output_dir):
        for c in closures:
            data = load_pack(output_dir, c.pack_id)
            if data is None:
                continue
            pack = AgentCodingPack.from_dict(data, fallback_id=c.pack_id)
            if pack.status != c.from_status:  # changed since planning
                continue
            pack.apply_status("obsolete", f"gc ({c.rule}): {c.reason}"[:2000])
            pack.governance["gc"] = {"rule": c.rule, "reason": c.reason, "at": now, "from": c.from_status}
            write_pack(pack, output_dir, preserve_status=False)
            closed.append(pack)
        upsert_index_entries(output_dir, closed, public_base_url=public_base_url)
    for pack in closed:  # network calls, outside the lock
        refresh_pack_rag(output_dir, pack.to_dict())  # closed packs leave the RAG collection
    return len(closed)


def run(output_dir: str | Path | None = None, *, dry_run: bool = False, force: bool = False, repo: str = "",
        session=None, mirrors_path: str | Path = "config/repo_mirrors.yaml", now: datetime | None = None,
        public_base_url: str = "") -> dict[str, Any]:
    """Plan (and unless *dry_run*, apply) a GC pass; writes ``pack_gc.json``."""
    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    now = now or datetime.now(UTC)
    packs = load_packs(out)
    if session is None:
        from auditor import ui

        with ui._db_session() as s:
            scans = load_scans(s)
    else:
        scans = load_scans(session)
    mirrors = {name: Mirror(path) for name, path in mirror_paths(mirrors_path).items()}
    misses = _int_env("AI_AUDIT_PACK_GC_MISSES", 3)
    ttl = _int_env("AI_AUDIT_PACK_GC_DIFF_TTL_DAYS", 30)
    p = plan(packs, scans, mirrors, now=now, misses=misses, ttl_days=ttl, force=force, repo=repo)
    closed = 0 if dry_run else apply(out, p["closures"], public_base_url=public_base_url)
    by_rule: dict[str, int] = defaultdict(int)
    by_repo: dict[str, int] = defaultdict(int)
    for c in p["closures"]:
        by_rule[c.rule] += 1
        by_repo[c.repo] += 1
    report = {
        "at": now.isoformat(), "dry_run": dry_run, "force": force, "misses": misses, "ttl_days": ttl,
        "open_considered": p["open_considered"], "wontfix_considered": p["wontfix_considered"],
        "planned": len(p["closures"]), "closed": closed,
        "by_rule": dict(by_rule), "by_repo": dict(sorted(by_repo.items())),
        "held": p["held"], "skipped": p["skipped"],
        "closures": [asdict(c) for c in p["closures"]],
    }
    if not dry_run:
        atomic_write_text(out / REPORT_NAME, json.dumps(report, indent=1))
    return report


def run_quietly(output_dir: str | Path | None = None, **kw: Any) -> dict[str, Any]:
    """Pipeline tail: never fail the caller over housekeeping."""
    if not enabled():
        return {"skipped": "AI_AUDIT_PACK_GC=0"}
    try:
        from conductor.publish import public_base_url

        r = run(output_dir, public_base_url=public_base_url(), **kw)
        return {k: r[k] for k in ("planned", "closed", "by_rule", "held", "skipped")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pack gc failed: %s", exc)
        return {"error": str(exc)[:300]}


# ── architecture packs ───────────────────────────────────────────────────────

ARCH_REPORT_NAME = "pack_gc_architecture.json"


def plan_architecture(entries: list[dict[str, Any]], reported: list[Any], scanned_repos: set[str], *,
                      force: bool = False) -> dict[str, Any]:
    """Which open architecture packs an ecosystem run no longer reports. Changes nothing.

    ``reported`` holds the run's architecture findings (``agent_pack.architecture_findings``),
    whose ids are the stable pack ids. An open pack outside them, in a repo the run scanned, is
    either a pack from before stable ids that repeats a reported issue (closed as its duplicate,
    ``wontfix`` with ``duplicate_of``, the same as finding-pack dedupe), or no longer reported
    (``obsolete``; a later report reopens it). Only ``open`` packs that aren't duplicates are
    considered. Closing more than half of them, and more than ``HOLD_MIN``, as obsolete is held
    unless ``force``: an empty or partial run looks the same as a fixed codebase.
    """
    from auditor.agent_pack import _LINE_SUFFIX

    reported_ids = {f.id for f in reported}
    by_title = {(f.repo, _LINE_SUFFIX.sub("", f.title)): f.id for f in reported}
    candidates = [e for e in entries if e.get("kind") == "architecture"
                  and (e.get("status") or "open") == "open" and not e.get("duplicate_of")
                  and str(e.get("repo") or "") in scanned_repos]
    closures: list[Closure] = []
    for e in candidates:
        if e["id"] in reported_ids:
            continue
        repo, title = str(e.get("repo") or ""), str(e.get("title") or "")
        keeper = by_title.get((repo, _LINE_SUFFIX.sub("", title)))
        if keeper:
            closures.append(Closure(e["id"], repo, str(e.get("file_path") or ""), title, "architecture",
                                    "duplicate", keeper))
        else:
            closures.append(Closure(e["id"], repo, str(e.get("file_path") or ""), title, "architecture",
                                    "not_reported", "the latest ecosystem run no longer reports this violation"))
    held = ""
    gone = [c for c in closures if c.rule == "not_reported"]
    if not force and len(gone) > HOLD_MIN and len(gone) > HOLD_SHARE * len(candidates):
        held = (f"would close {len(gone)} of {len(candidates)} open architecture packs as no longer "
                f"reported (> {HOLD_SHARE:.0%}); rerun with force")
        closures = [c for c in closures if c.rule != "not_reported"]
    return {"closures": closures, "held": held, "open_considered": len(candidates)}


def close_unreported_architecture(output_dir: str | Path, reported: list[Any], scanned_repos: set[str], *,
                                  force: bool = False, dry_run: bool = False,
                                  public_base_url: str = "") -> dict[str, Any]:
    """Plan and (unless *dry_run*) apply ``plan_architecture``; writes ``pack_gc_architecture.json``.
    Each pack is re-checked under the pack lock, so one claimed since planning is left alone."""
    from auditor.agent_pack import (
        AgentCodingPack,
        _index_entries,
        load_pack,
        pack_lock,
        refresh_pack_rag,
        upsert_index_entries,
        write_pack,
    )

    out = Path(output_dir)
    p = plan_architecture(_index_entries(out), reported, scanned_repos, force=force)
    closed: list[AgentCodingPack] = []
    if not dry_run and p["closures"]:
        now = datetime.now(UTC).isoformat()
        with pack_lock(out):
            for c in p["closures"]:
                data = load_pack(out, c.pack_id)
                if data is None:
                    continue
                pack = AgentCodingPack.from_dict(data, fallback_id=c.pack_id)
                if (pack.status or "open") != "open" or pack.governance.get("duplicate_of"):
                    continue  # changed since planning
                if c.rule == "duplicate":
                    pack.apply_status("wontfix", f"duplicate of {c.reason}")
                    pack.governance["duplicate_of"] = c.reason
                else:
                    pack.apply_status("obsolete", f"gc ({c.rule}): {c.reason}")
                    pack.governance["gc"] = {"rule": c.rule, "reason": c.reason, "at": now, "from": "open"}
                write_pack(pack, out, preserve_status=False)
                closed.append(pack)
            upsert_index_entries(out, closed, public_base_url=public_base_url)
        for pack in closed:  # network calls, outside the lock
            refresh_pack_rag(out, pack.to_dict())
    by_rule: dict[str, int] = defaultdict(int)
    for c in p["closures"]:
        by_rule[c.rule] += 1
    report = {"at": datetime.now(UTC).isoformat(), "dry_run": dry_run, "force": force,
              "reported": len(reported), "open_considered": p["open_considered"],
              "planned": len(p["closures"]), "closed": len(closed), "by_rule": dict(by_rule),
              "held": p["held"], "closures": [asdict(c) for c in p["closures"]]}
    if not dry_run:
        atomic_write_text(out / ARCH_REPORT_NAME, json.dumps(report, indent=1))
    return report
