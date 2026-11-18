"""validate_patch for TypeScript/JavaScript: run each touched npm project's own checks on the patched tree.

A changed ``.ts/.tsx/.js/...`` file maps to its nearest ``package.json`` (``web/``, ``frontend/``, or
the repo root). That project's dependencies are installed once per lockfile hash with
``npm ci --ignore-scripts`` into a cache, and the cache's ``node_modules`` is symlinked into the
temp tree. Then the scripts the project declares run in a fixed order: ``typecheck``, ``lint``,
every ``*:check`` (the Argus front ends' ``wire:check`` and similar drift checks, which read no
network), and ``test``.

A failing check is run again on the base tree, and only errors the patch introduced count. Errors
are compared as tsc diagnostics (file, code, message), eslint errors (file, rule, message), vitest
failures (test id) or, for any other script, output lines. Line numbers are left out, because a
patch shifts them.

A file that gets no check at all is reported in ``unvalidated``, and the caller doesn't treat the
patch as passed. That happens when npm is missing, the install fails, the project has no lockfile
or declares no checks, or a ``.ts`` file has no ``package.json`` above it. A plain ``.js`` file with
no project gets ``node --check`` (syntax only), like the Python fatal-only lint.
"""
from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from auditor.contracts import ValidationResult
from auditor import validate_cache

logger = logging.getLogger(__name__)

TOOL = "npm_checks"
SYNTAX_TOOL = "node_check"
TS_EXTS = (".ts", ".tsx", ".mts", ".cts")
JS_EXTS = (".js", ".jsx", ".mjs", ".cjs")
CODE_EXTS = TS_EXTS + JS_EXTS + (".vue", ".svelte")
LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json")
INSTALL_TIMEOUT_S = 900
_OUTPUT_LIMIT = 8000
_TYPECHECK_SCRIPTS = ("typecheck", "type-check", "tsc")
_PASS_WITH_NO_TESTS_RUNNERS = ("jest", "vitest", "playwright", "bun")
_NPM_DEFAULT_TEST = "no test specified"

_TSC_RE = re.compile(r"^(?P<file>\S.*?)\(\d+,\d+\): error (?P<code>TS\d+): (?P<msg>.*)$", re.MULTILINE)
_ESLINT_ROW_RE = re.compile(r"^\s+\d+:\d+\s+error\s+(?P<msg>.*?)\s{2,}(?P<rule>\S+)\s*$")
_VITEST_FAIL_RE = re.compile(r"^\s*FAIL\s+(?P<id>\S.*?)\s*$", re.MULTILINE)
_TIMING_RE = re.compile(r"\b\d+(?:\.\d+)?\s?m?s\b")
_ENV_GAP_RE = re.compile(r"(sh: \d+: \S+: not found|command not found|env: .?node.?: No such file)")


@dataclass
class NodeOutcome:
    """What the npm side found. ``ran``: (label, result) per project that ran at least one check."""

    ran: list[tuple[str, ValidationResult]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unvalidated: list[str] = field(default_factory=list)
    syntax_only: bool = False  # only `node --check` ran, no project checks


# ── Mapping changed files to projects ────────────────────────────────────────

def node_root(tree: Path, rel_path: str) -> Path | None:
    """Nearest directory with a ``package.json`` from *rel_path* up to *tree*, else None."""
    tree = tree.resolve()
    p = (tree / rel_path).parent
    while True:
        try:
            rel = p.resolve().relative_to(tree)
        except ValueError:
            return None
        if "node_modules" not in rel.parts and (p / "package.json").is_file():
            return p.resolve()
        if p.resolve() == tree:
            return None
        p = p.parent


def node_files(tree: Path, changed: list[str]) -> list[str]:
    """Changed files the npm side should look at: code files anywhere, and JSON inside an npm project.

    Deleted files count: removing a module breaks whatever imports it."""
    out = []
    for f in changed:
        if "node_modules/" in f:
            continue
        if f.endswith(CODE_EXTS) or (f.endswith(".json") and node_root(tree, f) is not None):
            out.append(f)
    return sorted(set(out))


# ── Environment ──────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _node_version() -> str:
    try:
        return subprocess.run(["node", "--version"], check=False, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _env(npm_cache: Path | None = None) -> dict[str, str]:
    """Child env. npm's cache must be writable: in the container the API runs as a uid with no
    passwd entry, so HOME is ``/`` and npm's default ``/.npm`` fails every install with EACCES."""
    env = {**os.environ, "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "NEXT_TELEMETRY_DISABLED": "1",
           "VITE_CONFIG_NATIVE_IGNORE_WARNING": "true", "npm_config_update_notifier": "false"}
    if npm_cache is not None:
        env["npm_config_cache"] = str(npm_cache)
    elif not env.get("npm_config_cache"):
        env["npm_config_cache"] = str(Path(tempfile.gettempdir()) / "ai-audit-npm-cache")
    return env


def ensure_node_modules(root: Path, cache_dir: Path) -> tuple[Path | None, str]:
    """``node_modules`` holding *root*'s locked deps, installed once per lockfile hash.

    ``npm ci --ignore-scripts``: install scripts never run, and the patched tree is not
    touched. Returns ``(node_modules, "")`` or ``(None, error)``."""
    lock = next((root / n for n in LOCKFILES if (root / n).is_file()), None)
    if lock is None:
        return None, "no package-lock.json (yarn and pnpm lockfiles aren't supported)"
    h = hashlib.sha256(_node_version().encode())
    for f in (root / "package.json", lock):
        h.update(f.read_bytes())
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = cache_dir / f"{root.name or 'repo'}-{h.hexdigest()[:16]}"
    modules = entry / "node_modules"
    with open(cache_dir / f".{entry.name}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # one install per lockfile; others wait, then reuse it
        if (entry / ".ready").exists():
            validate_cache.touch(entry)  # last use, for eviction
            return modules, ""
        shutil.rmtree(entry, ignore_errors=True)
        entry.mkdir(parents=True)
        for f in (root / "package.json", lock):
            shutil.copyfile(f, entry / f.name)
        try:
            subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=entry,
                           env=_env(cache_dir / ".npm-cache"),
                           check=True, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S)
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(entry, ignore_errors=True)
            return None, ((exc.stderr or exc.stdout or "").strip()[-800:] or str(exc))
        except (subprocess.TimeoutExpired, OSError) as exc:
            shutil.rmtree(entry, ignore_errors=True)
            return None, str(exc)
        (entry / ".ready").write_text(lock.name, encoding="utf-8")
        logger.info("installed node_modules %s for %s", entry.name, root)
        validate_cache.evict_in_background(cache_dir)
        return modules, ""


def _link_modules(root: Path, modules: Path) -> None:
    link = root / "node_modules"
    if not link.exists() and not link.is_symlink():
        link.symlink_to(modules, target_is_directory=True)


# ── Running checks ───────────────────────────────────────────────────────────

def project_checks(root: Path) -> list[tuple[str, list[str]]]:
    """``(script, npm args)`` for the checks *root* declares, in run order."""
    try:
        scripts = (json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {})
    except (OSError, ValueError):
        return []
    out: list[tuple[str, list[str]]] = []
    typecheck = next((s for s in _TYPECHECK_SCRIPTS if s in scripts), None)
    if typecheck:
        out.append((typecheck, ["run", "-s", typecheck]))
    if "lint" in scripts:
        out.append(("lint", ["run", "-s", "lint"]))
    out += [(s, ["run", "-s", s]) for s in sorted(scripts) if s.endswith(":check")]
    test = str(scripts.get("test") or "")
    if test and _NPM_DEFAULT_TEST not in test:
        args = ["run", "-s", "test"]
        if any(r in test.lower() for r in _PASS_WITH_NO_TESTS_RUNNERS):
            args += ["--", "--passWithNoTests"]
        out.append(("test", args))
    return out


def _run(cmd: list[str], cwd: Path, timeout_s: int, tool: str = TOOL) -> ValidationResult:
    """Run *cmd*; on timeout kill its whole process group (npm leaves node children behind)."""
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
    except OSError as exc:
        return ValidationResult(passed=False, tool=tool, duration_ms=0, errors=[str(exc)],
                                raw_output=f"env: node: No such file: {exc}")
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return ValidationResult(passed=False, tool=tool, duration_ms=int((time.monotonic() - t0) * 1000),
                                errors=[f"timed out after {timeout_s}s"], raw_output="")
    out = (out or "")[-_OUTPUT_LIMIT:]
    return ValidationResult(passed=proc.returncode == 0, tool=tool, duration_ms=int((time.monotonic() - t0) * 1000),
                            errors=[] if proc.returncode == 0 else [out.strip()[-1000:]], raw_output=out)


def error_ids(script: str, output: str, root: Path) -> Counter:
    """The errors in a check's *output*, keyed without line numbers or the tree's own path."""
    text = output
    for prefix in {str(root.resolve()) + os.sep, str(root) + os.sep}:
        text = text.replace(prefix, "")
    ids: Counter = Counter()
    tsc = list(_TSC_RE.finditer(text))
    if tsc:
        ids.update(f"{m['file']}: {m['code']}: {m['msg'].strip()}" for m in tsc)
        return ids
    current = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and re.match(r"^\S+\.\w+$", line):
            current = line.strip()
            continue
        m = _ESLINT_ROW_RE.match(line)
        if m and current:
            ids[f"{current}: {m['rule']}: {m['msg']}"] += 1
    if ids:
        return ids
    fails = [m["id"] for m in _VITEST_FAIL_RE.finditer(text)]
    if fails:
        ids.update(fails)
        return ids
    # anything else (a drift check): its output lines, timings dropped
    ids.update(ln for ln in (_TIMING_RE.sub("", x).strip() for x in text.splitlines()) if ln)
    return ids


def _against_base(script: str, args: list[str], root: Path, tree: Path, base_tree: Callable[[], Path | None],
                  cache_dir: Path, timeout_s: int, res: ValidationResult, label: str,
                  notes: list[str]) -> ValidationResult:
    """Re-judge a failing check by what the patch broke: errors also present without it don't count."""
    patched = error_ids(script, res.raw_output, root)
    base = base_tree()
    if base is None or not patched:
        return res
    base_root = base.resolve() / root.relative_to(tree.resolve())
    if not (base_root / "package.json").is_file():
        return res
    modules, _ = ensure_node_modules(base_root, cache_dir)
    if modules is None:
        return res
    _link_modules(base_root, modules)
    base_res = _run(["npm", *args], base_root, timeout_s)
    if base_res.passed:
        return res
    new = patched - error_ids(script, base_res.raw_output, base_root)
    if new:
        res.raw_output = (f"new {script} errors from this patch: {'; '.join(sorted(new))[:1500]}\n"
                          + res.raw_output)[-_OUTPUT_LIMIT:]
        return res
    notes.append(f"{label}: {script} fails on the base branch too ({sum(patched.values())} error(s)), not counted")
    return ValidationResult(passed=True, tool=TOOL, duration_ms=res.duration_ms + base_res.duration_ms,
                            errors=[], raw_output=res.raw_output)


def _env_gap_is_the_cause(root: Path, script: str, output: str) -> bool:
    """True when *script* couldn't start here: node itself is missing, or the command the script
    runs first isn't installed. A missing command reported from inside a running check is not."""
    if re.search(r"env: .?node.?: No such file", output):
        return True
    try:
        cmd = str((json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {})
                  .get(script) or "")
    except (OSError, ValueError):
        return False
    first = next((t for t in cmd.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)), "")
    if not first:
        return False
    b = re.escape(first)
    return bool(re.search(rf"(^|\n)(sh: \d+: {b}: not found|(\S+: )*(line \d+: )?{b}: command not found)", output))


def _syntax_check(tree: Path, files: list[str], timeout_s: int) -> ValidationResult:
    """``node --check`` each plain JS file: syntax only, the JS counterpart of the fatal-only ruff lint."""
    failed: list[str] = []
    total = 0
    tree_r = tree.resolve()
    for f in files:
        if not (tree / f).resolve().is_relative_to(tree_r):
            failed.append(f"{f}: resolves outside the tree, not checked")
            continue
        # "--" ends node's options: a file named "--require=x.js" must be a path, not an option.
        r = _run(["node", "--check", "--", f], tree, timeout_s, tool=SYNTAX_TOOL)
        total += r.duration_ms
        if not r.passed:
            failed.append(r.raw_output.strip())
    return ValidationResult(passed=not failed, tool=SYNTAX_TOOL, duration_ms=total, errors=failed[:5],
                            raw_output="\n".join(failed)[-_OUTPUT_LIMIT:])


# ── Entry point ──────────────────────────────────────────────────────────────

def validate_node_projects(
    tree: Path, files: list[str], *, timeout_s: int = 120, cache_dir: Path,
    base_tree: Callable[[], Path | None] | None = None,
) -> NodeOutcome:
    """Check every npm project *files* touch, plus a syntax check of plain JS outside any project."""
    out = NodeOutcome()
    if shutil.which("npm") is None or shutil.which("node") is None:
        out.unvalidated.append(f"node/npm not installed, so {', '.join(files[:5])} got no check")
        return out
    roots: dict[Path, list[str]] = {}
    loose_js: list[str] = []
    for f in files:
        root = node_root(tree, f)
        if root is not None:
            roots.setdefault(root, []).append(f)
        elif f.endswith(JS_EXTS) and (tree / f).is_file():
            loose_js.append(f)
        elif f.endswith(TS_EXTS + (".vue", ".svelte")):
            out.unvalidated.append(f"{f}: no package.json above it, so nothing can typecheck it")
    if loose_js:
        r = _syntax_check(tree, loose_js, timeout_s)
        out.ran.append(("node --check", r))
        out.notes.append(f"no npm project for {', '.join(loose_js)}: syntax check only")
    tree_r = tree.resolve()
    for root, fs in sorted(roots.items()):
        label = str(root.relative_to(tree_r)) if root != tree_r else "."
        checks = project_checks(root)
        if not checks:
            out.unvalidated.append(f"{label}: package.json declares no typecheck, lint, *:check or test script")
            continue
        modules, err = ensure_node_modules(root, cache_dir)
        if modules is None:
            out.unvalidated.append(f"{label}: npm ci failed, so its checks didn't run: {err[:300]}")
            continue
        _link_modules(root, modules)
        results: list[tuple[str, ValidationResult]] = []
        for script, args in checks:
            res = _run(["npm", *args], root, timeout_s)
            if not res.passed and _env_gap_is_the_cause(root, script, res.raw_output):
                line = next((ln for ln in res.raw_output.splitlines() if _ENV_GAP_RE.search(ln)), "")
                out.notes.append(f"{label}: {script} can't run here, skipped: {line.strip()[:200]}")
                continue
            # Any other failure, even one whose output mentions a missing command (a patch can break
            # an import or spawn a missing binary), is judged against the base tree: it is only
            # discounted when the base fails the same way.
            if not res.passed and base_tree is not None:
                res = _against_base(script, args, root, tree, base_tree, cache_dir, timeout_s, res, label,
                                    out.notes)
            results.append((script, res))
        if not results:
            out.unvalidated.append(f"{label}: none of its checks could run")
            continue
        failed = [(s, r) for s, r in results if not r.passed]
        merged = ValidationResult(
            passed=not failed, tool=TOOL, duration_ms=sum(r.duration_ms for _, r in results),
            errors=[f"{label} {s}: {e}" for s, r in failed for e in r.errors][:10],
            raw_output=("\n".join(f"-- {label} npm run {s}\n{r.raw_output}" for s, r in failed)
                        if failed else ", ".join(s for s, _ in results))[-_OUTPUT_LIMIT:],
        )
        out.ran.append((label, merged))
    out.syntax_only = bool(out.ran) and all(lbl == "node --check" for lbl, _ in out.ran)
    return out
