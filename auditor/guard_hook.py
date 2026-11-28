"""Write-time guard check: a Claude Code ``PostToolUse`` hook that runs the proven deterministic rules
on a file an agent just edited and tells the agent about hits on the lines it changed.

Only rules that have earned trust run: argus-ci's suite *guard* rules (the same ``check.py`` and
catalogue as CI, gated by ``rules.fixtures``; convention rules stay out), and the static rule of every
class guard whose proof passed (``class_packs.prove``), from whichever repo proved it. Python only,
like the catalogue.

A hit is shown only when its line is in the file's ``git diff -U0 HEAD`` (or the file is new), so
baselined debt never reaches the agent. The output is advice and never evidence: nothing here
writes a pack, and nothing on the evidence side reads it. A check that can't run (no catalogue, no
ruff, a crash) exits 0 and shows nothing, so a broken hook never blocks editing; that run is logged
as ``unavailable`` (or ``partial`` when only some rules ran) and never counts as a fix.

Each run appends to ``<output_dir>/guard_hook_history.jsonl``: every rule's whole-file hit count,
and how many of those were shown. ``write_time_metric`` reads it for the informational
``guards.write_time`` gate: of the hits shown, the share gone from the file by the next check of the
same file. That compares whole-file counts, so committing the change (which empties the diff) or
rewording the line can't count as a fix. A reasoned ``# noqa`` counts as handled, as in CI.

Hook wiring (written by ``scripts/install_agent_pack_awareness.sh`` into each repo's
``.claude/settings.local.json``)::

    {"hooks": {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit",
      "hooks": [{"type": "command", "command": "<venv python> -m auditor.guard_hook", "timeout": 60}]}]}}
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]  # this repo: the hook runs with another repo as its cwd
HISTORY_NAME = "guard_hook_history.jsonl"
HOOK_MODULE = "auditor.guard_hook"
WINDOW_DAYS = 14
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


@dataclass(frozen=True)
class Hit:
    rule: str     # "suite:missing-timeout" or "ruff:S113"
    line: int
    message: str
    text: str


def output_dir() -> Path:
    return Path(os.getenv("AI_AUDIT_OUTPUT_DIR") or ROOT / "reports")


# ── which rules ──────────────────────────────────────────────────────────────

def proven_rules(out: Path) -> list[dict[str, Any]]:
    """Static rules of class guards whose proof passed, in any repo, each with its class. Reads only
    the class packs (``pack-class-*.yaml``), never the whole pack set: this runs on every edit."""
    import yaml

    from auditor.agent_pack import packs_dir

    rules: dict[tuple[str, ...], dict[str, Any]] = {}
    for f in sorted(packs_dir(out).glob("pack-class-*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        gov = data.get("governance") if isinstance(data, dict) else None
        cls = str(((gov or {}).get("finding_class") or {}).get("id") or f.stem.removeprefix("pack-class-"))
        for rec in ((gov or {}).get("class_guard") or {}).values():
            if not isinstance(rec, dict) or (rec.get("proof") or {}).get("status") != "proven":
                continue
            rule = rec.get("rule")
            if isinstance(rule, dict) and rule.get("tool") == "ruff" and isinstance(rule.get("select"), list):
                sel = tuple(sorted(str(c) for c in rule["select"]))
                rules.setdefault(sel, {"tool": "ruff", "select": list(sel), "class": cls})
    return list(rules.values())


# ── running them ─────────────────────────────────────────────────────────────

def suite_hits(repo: str, root: Path, rel: str, out: Path) -> list[Hit] | None:
    """Suite guard-rule hits in *rel*; None when the catalogue isn't there (never "no hits")."""
    from auditor import suite_rules

    cdir = suite_rules.catalogue_dir(ROOT / "config" / "repo_mirrors.yaml", cache_root=out / "_suite_rules")
    if cdir is None:
        return None
    found = suite_rules.scan_or_none(repo, root, [rel], cdir=cdir)
    if found is None:
        return None
    hits = []
    for f in found:
        rule = f.title.split(":", 1)[0]
        hits.append(Hit(f"suite:{rule}", f.line, f.description, f.evidence))
    return hits


def ruff_hits(path: Path, rule: dict[str, Any]) -> list[Hit] | None:
    """*rule*'s hits in *path*, honouring ``# noqa`` as CI does; None when ruff can't run."""
    exe = shutil.which("ruff") or str(Path(sys.executable).with_name("ruff"))
    if not Path(exe).is_file():
        return None
    try:
        r = subprocess.run([exe, "check", "--isolated", "--no-cache", "--output-format", "json",
                            "--select", ",".join(rule["select"]), str(path)],
                           check=False, capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout or "null")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if r.returncode not in (0, 1) or not isinstance(data, list):
        return None
    if any(not isinstance(h, dict) or not h.get("code") or h.get("code") == "E999" for h in data):
        return None  # unparseable: never checked, which must not read as clean
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for h in data:
        row = int((h.get("location") or {}).get("row") or 0)
        if any(h["code"].startswith(s) for s in rule["select"]) and 0 < row <= len(lines):
            out.append(Hit(f"ruff:{h['code']}", row,
                           f"{h.get('message') or h['code']} (class guard for {rule.get('class', '?')})",
                           lines[row - 1].strip()))
    return out


def changed_lines(root: Path, rel: str) -> set[int] | None:
    """Lines of *rel* that differ from HEAD; None means every line (new, untracked, or no git)."""
    def git(*a: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=30, check=False)

    try:
        if git("rev-parse", "--verify", "-q", "HEAD").returncode != 0:
            return None
        if git("ls-files", "--error-unmatch", "--", rel).returncode != 0:
            return None  # untracked: all of it is the agent's
        d = git("diff", "-U0", "--no-color", "--no-ext-diff", "HEAD", "--", rel)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if d.returncode != 0:
        return None
    out: set[int] = set()
    for m in _HUNK.finditer(d.stdout):
        start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        out.update(range(start, start + n))
    return out


# ── the hook ─────────────────────────────────────────────────────────────────

Runner = Callable[[str, Path, str, Path], "list[Hit] | None"]


def check(file_path: str, cwd: str, *, out: Path | None = None, suite: Runner | None = None,
          rules: list[dict[str, Any]] | None = None, record: bool = True) -> tuple[list[Hit], dict[str, Any]]:
    """Hits to show for *file_path*, and the history row. Never raises."""
    out = out or output_dir()
    path = Path(file_path if os.path.isabs(file_path) else os.path.join(cwd or ".", file_path))
    row: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}
    if path.suffix != ".py" or not path.is_file():
        return [], row
    try:
        top = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=30, check=False)
        root = Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else path.parent
    except (OSError, subprocess.TimeoutExpired):
        root = path.parent
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        root, rel = path.parent, path.name
    repo = root.name
    row.update({"repo": repo, "file": rel})

    found: list[Hit] = []
    ran = 0
    s = (suite or suite_hits)(repo, root, rel, out)
    if s is not None:
        found += s
        ran += 1
    suite_lines = {h.line for h in found}
    rules = proven_rules(out) if rules is None else rules
    for rule in rules:
        r = ruff_hits(path, rule)
        if r is None:
            continue
        ran += 1
        found += [h for h in r if h.line not in suite_lines]  # one report per line; the suite rule says it best
    if not ran:
        row["status"] = "unavailable"
        _append(out, row, record)
        return [], row

    changed = changed_lines(root, rel)
    shown = [h for h in found if changed is None or h.line in changed]
    counts: dict[str, dict[str, int]] = {}
    for h in found:
        counts.setdefault(h.rule, {"total": 0, "shown": 0})["total"] += 1
    for h in shown:
        counts[h.rule]["shown"] += 1
    # "partial": some source couldn't run, so its rules' counts are missing, not zero; the metric
    # compares "ok" runs only, so a source that failed never reads as the hit being fixed.
    row.update({"status": "ok" if ran == 1 + len(rules) else "partial", "rules": counts})
    _append(out, row, record)
    return sorted(shown, key=lambda h: h.line), row


def _append(out: Path, row: dict[str, Any], record: bool) -> None:
    if not record or "repo" not in row:
        return
    try:
        out.mkdir(parents=True, exist_ok=True)
        with (out / HISTORY_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.info("guard hook: history not written: %s", exc)


def render(hits: list[Hit], rel: str) -> str:
    lines = [(f"Guard rules flagged {len(hits)} line(s) you changed in {rel} "
              "(deterministic rules this suite enforces; CI fails on new hits):")]
    for h in hits:
        lines.append(f"- {rel}:{h.line} [{h.rule}] {h.message}\n    {h.text[:160]}")
    lines.append("Fix them, or add a `# noqa: <code>` / `# nosemgrep: <id>` with the reason on that line.")
    return "\n".join(lines)


def main(stdin: Any = None) -> int:
    """Hook entry point. Exit 2 with the hits on stderr (Claude Code shows them to the agent); exit 0
    otherwise, including whenever anything goes wrong."""
    try:
        payload = json.load(stdin or sys.stdin)
        ti = payload.get("tool_input") or {}
        fp = ti.get("file_path") or ti.get("path") or ""
        if not isinstance(fp, str) or not fp:
            return 0
        hits, row = check(fp, str(payload.get("cwd") or os.getcwd()))
    except Exception as exc:  # noqa: BLE001 - a hook must never block editing
        logger.info("guard hook failed: %s", exc)
        return 0
    if not hits:
        return 0
    print(render(hits, row.get("file", fp)), file=sys.stderr)
    return 2


# ── measuring it ─────────────────────────────────────────────────────────────

def load_history(out: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        text = (out / HISTORY_NAME).read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def write_time_metric(out: Path, *, now: datetime | None = None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Of the hits shown in the window, the share gone by the next ``ok`` check of the same file
    (whole-file count of that rule fell by at least what was shown). A shown hit with no later check
    yet isn't counted; an ``unavailable`` run is skipped, never read as clean."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    by_file: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in load_history(out):
        try:
            at = datetime.fromisoformat(str(r.get("at")))
        except ValueError:
            continue
        if (at if at.tzinfo else at.replace(tzinfo=UTC)) < cutoff or r.get("status") != "ok":
            continue
        by_file.setdefault((str(r.get("repo")), str(r.get("file"))), []).append(r)
    shown = handled = 0
    by_rule: dict[str, dict[str, int]] = {}
    for runs in by_file.values():
        for a, b in pairwise(runs):
            for rule, c in (a.get("rules") or {}).items():
                s = int(c.get("shown") or 0)
                if not s:
                    continue
                after = int(((b.get("rules") or {}).get(rule) or {}).get("total") or 0)
                ok = after <= int(c.get("total") or 0) - s
                shown += 1
                handled += ok
                br = by_rule.setdefault(rule, {"shown": 0, "handled": 0})
                br["shown"] += 1
                br["handled"] += ok
    return {"n": shown, "handled": handled, "rate": round(handled / shown, 4) if shown else None,
            "by_rule": by_rule, "window_days": days}


if __name__ == "__main__":
    raise SystemExit(main())
