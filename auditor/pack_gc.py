"""Pack GC: close finding packs whose issue the scanners no longer report, as ``obsolete`` (#67).

Nothing else closes a finding pack except an agent or a person, so packs for issues fixed outside
the pack loop (a batch PR, a refactor, a deleted file) stay open forever and add noise to bundles,
the board and every trust metric. ``obsolete`` means "no longer reported". It is not a fix
(``resolved``) and not a decision (``wontfix``). A pack closed this way reopens when the issue is
found again (``agent_pack._carry_over_lifecycle``), and the trust metrics count those reopenings
as the GC's false-close rate.

Only ``open`` packs are considered. Claimed, in-progress and unverified packs have someone on
them or a fix in hand, so they are never touched.

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
packs, and more than ``HOLD_MIN``, is held unless ``force``.
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
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, timeout=60, check=False)

    @property
    def ok(self) -> bool:
        return bool(self.path and self.ref)

    def files(self) -> set[str]:
        if not self.ok:  # never run git in the process CWD or against the index
            return set()
        if self._files is None:
            r = self._git("ls-tree", "-r", "--name-only", self.ref)
            self._files = set(r.stdout.splitlines()) if r.returncode == 0 else set()
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


def mirror_paths(mirrors_path: str | Path = "config/repo_mirrors.yaml") -> dict[str, Path]:
    from auditor.trust_metrics import _mirror_paths

    return _mirror_paths(mirrors_path)


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
    open_by_repo: dict[str, int] = defaultdict(int)
    closures: list[Closure] = []
    skipped: dict[str, str] = {}
    for p in packs:
        if not p.is_finding or p.status != "open" or p.duplicate_of or not p.path or (repo and p.repo != repo):
            continue
        open_by_repo[p.repo] += 1
        seq = scans_by_repo.get(p.repo, [])
        if _broken_latest_scan(seq):
            skipped[p.repo] = f"latest scan {seq[-1].run_id} reports nothing; the one before it didn't"
        m = mirrors.get(p.repo)
        c = _judge(p, seq if p.repo not in skipped else [], m, now=now, misses=misses, ttl_days=ttl_days)
        if c:
            closures.append(c)
    held: dict[str, str] = {}
    if not force:
        per_repo: dict[str, int] = defaultdict(int)
        for c in closures:
            per_repo[c.repo] += 1
        for r, n in per_repo.items():
            if n > HOLD_MIN and n > HOLD_SHARE * open_by_repo[r]:
                held[r] = f"would close {n} of {open_by_repo[r]} open packs (> {HOLD_SHARE:.0%}); rerun with force"
        closures = [c for c in closures if c.repo not in held]
    return {"closures": closures, "held": held, "skipped": skipped, "open_considered": sum(open_by_repo.values())}


def _judge(p: PackView, seq: list[Scan], m: Mirror | None, *, now: datetime, misses: int,
           ttl_days: int) -> Closure | None:
    def close(rule: str, reason: str) -> Closure:
        return Closure(p.id, p.repo, p.path, p.title, p.kind, rule, reason)

    if m is not None and m.ok and p.path not in m.files():
        return close("file_gone", f"{p.path} is not on {m.ref}")
    if p.kind in HUMAN_KINDS:
        return None
    seen = p.detected_at
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
    if m is not None and m.ok and seen and (core := cited_core(p.snippet, p.line)):
        current = m.lines(p.path)
        if current is not None and not code_still_there(core, current):
            if not nightly_covers(p.path):
                return close("code_changed", f"the cited lines around L{p.line} are gone from {m.ref} "
                             "(a file the nightly scan doesn't read)")
            if later and not any(_reported(p, s) for s in later):
                return close("code_changed", f"the cited lines around L{p.line} are gone from {m.ref}, and "
                             f"{len(later)} nightly scan(s) since {seen:%Y-%m-%d} don't report it")
    if p.kind in TTL_KINDS and seen and now - seen > timedelta(days=ttl_days):
        return close("expired", f"open {(now - seen).days} days since last seen (TTL {ttl_days})")
    return None


# ── applying ─────────────────────────────────────────────────────────────────

def apply(output_dir: str | Path, closures: list[Closure], *, public_base_url: str = "") -> int:
    """Set each pack ``obsolete`` with the reason in its history and ``governance.gc``."""
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
            if pack.status != "open":  # changed since planning
                continue
            pack.apply_status("obsolete", f"gc ({c.rule}): {c.reason}"[:2000])
            pack.governance["gc"] = {"rule": c.rule, "reason": c.reason, "at": now}
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
        "open_considered": p["open_considered"], "planned": len(p["closures"]), "closed": closed,
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
