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
# node:test's TAP reporter (`node --test`): a failing test or subtest, indented by depth. A `# TODO` failure doesn't
# fail the run. Its YAML blocks carry `duration_ms: 0.14` with no unit, so the plain-lines fallback read a new id
# into every run: argus-realms' arena tests failed every patch as "new test errors" that were only timings.
_TAP_FAIL_RE = re.compile(r"^\s*not ok \d+ (?:- )?(?P<id>.*?)\s*$", re.MULTILINE)
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
    from auditor.proc import child_env

    env = child_env(CI="1", NO_COLOR="1", FORCE_COLOR="0", NEXT_TELEMETRY_DISABLED="1",
                    VITE_CONFIG_NATIVE_IGNORE_WARNING="true", npm_config_update_notifier="false")
    if npm_cache is not None:
        env["npm_config_cache"] = str(npm_cache)
    elif not env.get("npm_config_cache"):
        env["npm_config_cache"] = str(Path(tempfile.gettempdir()) / "ai-audit-npm-cache")
    return env


_DEFAULT_REGISTRY = "https://registry.npmjs.org"
_DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url.strip())
        return f"{u.scheme.lower()}://{u.netloc.lower()}" if u.scheme in ("http", "https") and u.netloc else ""
    except ValueError:
        return ""


def allowed_registries() -> set[str]:
    """Origins ``npm ci`` may download from: npmjs, the configured ``npm_config_registry``, and any
    in ``AI_AUDIT_NPM_REGISTRIES`` (comma-separated)."""
    urls = [_DEFAULT_REGISTRY, os.getenv("npm_config_registry") or "",
            *(os.getenv("AI_AUDIT_NPM_REGISTRIES") or "").split(",")]
    return {o for o in map(_origin, urls) if o}


def _lock_entries(lock: dict) -> list[tuple[str, dict]]:
    """``(name, entry)`` for every package a lockfile names: v2/v3 ``packages``, v1 nested ``dependencies``."""
    out = [(k, v) for k, v in (lock.get("packages") or {}).items() if isinstance(v, dict)] \
        if isinstance(lock.get("packages"), dict) else []
    stack = [lock.get("dependencies")]
    while stack:
        deps = stack.pop()
        if not isinstance(deps, dict):
            continue
        for k, v in deps.items():
            if isinstance(v, dict):
                out.append((k, v))
                stack.append(v.get("dependencies"))
    return out


def _lock_key_in_node_modules(key: str) -> bool:
    """Whether a lockfile ``packages`` key is only ``node_modules/<name>`` or ``node_modules/@scope/<name>``
    pairs. npm reifies each entry at its key, so ``node_modules/../../x``, an absolute path or a
    backslash would put a tarball outside node_modules, or outside the checkout."""
    parts, i = key.split("/"), 0
    while i < len(parts):
        if parts[i] != "node_modules":
            return False
        width = 2 if i + 1 < len(parts) and parts[i + 1].startswith("@") else 1
        names = parts[i + 1:i + 1 + width]
        if len(names) != width or any(not n or n.startswith(".") or "\\" in n or n == "@" for n in names):
            return False
        i += 1 + width
    return True


def install_refusal(pkg_bytes: bytes, lock_bytes: bytes) -> str:
    """Why the audited repo's manifests must not reach ``npm ci``, or "" when they may.

    ``--ignore-scripts`` stops lifecycle scripts, not what npm fetches: an untrusted lockfile could
    send it to any host (``resolved`` URLs, git remotes) or link host paths into node_modules
    (``file:``/``link:`` specs, workspaces). Only registry tarballs from :func:`allowed_registries` pass."""
    try:
        pkg, lock = json.loads(pkg_bytes or b"{}"), json.loads(lock_bytes or b"{}")
    except ValueError:
        return "package.json or the lockfile isn't valid JSON"
    if not isinstance(pkg, dict) or not isinstance(lock, dict):
        return "package.json or the lockfile isn't a JSON object"
    if pkg.get("workspaces"):
        return "npm workspaces aren't supported (they link local paths)"
    for field_ in _DEP_FIELDS:
        deps = pkg.get(field_)
        for name, spec in (deps.items() if isinstance(deps, dict) else []):
            spec = str(spec).strip()
            if not spec.startswith("npm:") and (":" in spec or "/" in spec):  # url, path, git, user/repo
                return f"{field_} {name!r} is not a registry version ({spec[:80]!r})"
    packages = lock.get("packages") if isinstance(lock.get("packages"), dict) else {}
    for name in packages:  # "" is the project; anything else outside node_modules is a local dir
        if name and not _lock_key_in_node_modules(str(name)):
            return f"lockfile package {str(name)[:80]!r} is a local path"
    allowed = allowed_registries()
    for name, meta in _lock_entries(lock):
        if meta.get("link"):
            return f"lockfile package {name[:80]!r} is a local link"
        resolved = meta.get("resolved")
        if resolved is not None and _origin(str(resolved)) not in allowed:
            return f"lockfile package {name[:80]!r} resolves outside the allowed registries ({str(resolved)[:80]!r})"
        version = str(meta.get("version") or "")
        if ":" in version and not version.startswith("npm:"):
            return f"lockfile package {name[:80]!r} is not a registry version ({version[:80]!r})"
    return ""


def ensure_node_modules(root: Path, cache_dir: Path) -> tuple[Path | None, str]:
    """``node_modules`` holding *root*'s locked deps, installed once per lockfile hash.

    ``npm ci --ignore-scripts``: install scripts never run, and the patched tree is not
    touched. Returns ``(node_modules, "")`` or ``(None, error)``."""
    lock = next((root / n for n in LOCKFILES if (root / n).is_file() or (root / n).is_symlink()), None)
    if lock is None:
        return None, "no package-lock.json (yarn and pnpm lockfiles aren't supported)"
    pkg = root / "package.json"
    for f in (pkg, lock):
        # The audited repo is untrusted: a symlinked manifest could point at a host file, whose
        # contents would be copied into the cache and quoted back in npm's parse errors.
        if f.is_symlink():
            return None, f"{f.name} is a symlink, not followed"
        if not f.is_file():
            return None, f"no {f.name}"
    h = hashlib.sha256(_node_version().encode())
    try:
        manifests = [f.read_bytes() for f in (pkg, lock)]
    except OSError as exc:
        return None, str(exc)
    if refusal := install_refusal(*manifests):
        return None, f"not installed: {refusal}"
    for data in manifests:
        h.update(data)
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = cache_dir / f"{root.name or 'repo'}-{h.hexdigest()[:16]}"
    modules = entry / "node_modules"
    with open(cache_dir / f".{entry.name}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # one install per lockfile; others wait, then reuse it
        if (entry / ".ready").exists():
            validate_cache.touch(entry)  # last use, for eviction
            return modules, ""
        try:
            shutil.rmtree(entry, ignore_errors=True)  # a half-written entry from a killed install
            entry.mkdir(parents=True, exist_ok=True)  # rmtree may have left some of it behind
            for f in (pkg, lock):
                shutil.copyfile(f, entry / f.name, follow_symlinks=False)
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
    """Point *root*'s ``node_modules`` at *modules*. A symlink to anything else (a dangling link to
    an evicted cache entry, or another lockfile's deps) is replaced; a real directory is left alone."""
    link = root / "node_modules"
    if link.is_symlink():
        if os.readlink(link) == str(modules):
            return
        link.unlink(missing_ok=True)
    elif link.exists():
        return
    try:
        link.symlink_to(modules, target_is_directory=True)
    except FileExistsError:  # a concurrent run linked it first
        pass


# ── Running checks ───────────────────────────────────────────────────────────

def _scripts(root: Path) -> dict[str, str]:
    """The ``scripts`` table of *root*'s package.json; {} when it's missing, unreadable or not an
    object (valid JSON such as ``[]`` or ``"scripts": "x"`` included). Non-string commands are dropped."""
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(v, str)}


def project_checks(root: Path) -> list[tuple[str, list[str]]]:
    """``(script, npm args)`` for the checks *root* declares, in run order."""
    scripts = _scripts(root)
    if not scripts:
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
                                text=True, encoding="utf-8", errors="replace", start_new_session=True)
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
        try:  # bounded: a descendant that left the group (setsid) can hold the pipe open forever
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait()
        return ValidationResult(passed=False, tool=tool, duration_ms=int((time.monotonic() - t0) * 1000),
                                errors=[f"timed out after {timeout_s}s"], raw_output="")
    out = (out or "")[-_OUTPUT_LIMIT:]
    return ValidationResult(passed=proc.returncode == 0, tool=tool, duration_ms=int((time.monotonic() - t0) * 1000),
                            errors=[] if proc.returncode == 0 else [out.strip()[-1000:]], raw_output=out)


def error_ids(script: str, output: str, root: Path) -> Counter:
    """The errors in a check's *output*, keyed without line numbers or the tree's own path."""
    text = output
    # longest first, in a fixed order: a relative root's "repo/" is a substring of the resolved
    # "/home/u/repo/", and stripping it first would leave "/home/u/" in every id
    for prefix in sorted({str(root.resolve()) + os.sep, str(root) + os.sep}, key=len, reverse=True):
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
    tap = [m["id"] for m in _TAP_FAIL_RE.finditer(text) if not re.search(r"#\s*TODO\b", m["id"], re.IGNORECASE)]
    if tap:
        ids.update(re.sub(r"\s+#\s*SKIP\b.*$", "", i, flags=re.IGNORECASE) for i in tap)
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
    cmd = _scripts(root).get(script) or ""
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
