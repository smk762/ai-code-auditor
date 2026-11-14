"""Per-repo validation runner: detect language, run test suite, return ValidationResult.

Designed to be called both on live repo paths and on temporary copies (used by the
repair loop to test a patch before surfacing it to the user).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from auditor.config import RepoConfig
from auditor.contracts import ValidationResult
from auditor.repo_resolver import resolve_repo_path

logger = logging.getLogger(__name__)

_OUTPUT_LIMIT = 8000  # chars — cap raw_output to avoid huge payloads
_SUMMARY_LINE_LIMIT = 20  # max lines returned by extract_failure_summary


def validate_repo(
    repo: RepoConfig,
    path_override: Path | None = None,
    timeout_s: int = 120,
) -> ValidationResult:
    """Run the appropriate test/lint suite for *repo*.

    Args:
        repo: repository config (used when *path_override* is absent).
        path_override: use this path instead of the config path — for testing
            a patched temp copy without modifying the config.
        timeout_s: wall-clock timeout for the validation command.

    Gracefully returns a ``skipped`` result when:
    - The repo path is not writable (e.g. SSHFS read-only mount).
    - No test runner is installed.
    - The language / project type is unrecognised.
    """
    repo_path = path_override if path_override is not None else resolve_repo_path(repo)

    if not repo_path.exists():
        return _skipped(f"path not found: {repo_path}")

    if not _is_writable(repo_path):
        return _skipped("read-only mount — cannot run test suite in-place")

    lang = _detect_language(repo_path)
    if lang is None:
        return _skipped("no recognised project type (no pyproject.toml, package.json, go.mod, or Cargo.toml)")

    if lang == "python":
        return _run_python(repo_path, timeout_s)
    if lang == "node":
        return _run_node(repo_path, timeout_s)
    if lang == "go":
        return _run_go(repo_path, timeout_s)
    if lang == "rust":
        return _run_rust(repo_path, timeout_s)

    return _skipped(f"no validator for language {lang!r}")


# ── Language runners ──────────────────────────────────────────────────────────

def _run_python(repo_path: Path, timeout_s: int) -> ValidationResult:
    pytest_bin = _find_python_runner(repo_path)
    if pytest_bin is None:
        # Fall back to flake8/ruff lint only
        return _run_lint_only_python(repo_path, timeout_s)
    cmd = pytest_bin + ["-x", "--tb=short", "-q"]
    return _exec(cmd, cwd=repo_path, tool="pytest", timeout_s=timeout_s)


def _run_node(repo_path: Path, timeout_s: int) -> ValidationResult:
    if shutil.which("npm") is None:
        return _skipped("npm not installed")
    cmd = ["npm", "test", "--"]
    # --passWithNoTests is only honoured by Jest, Vitest, Playwright Test and
    # Bun. Mocha/AVA/node:test will reject the flag and fail the suite, so only
    # forward it when the test script clearly uses a supporting runner.
    if _node_runner_supports_pass_with_no_tests(repo_path):
        cmd.append("--passWithNoTests")
    return _exec(cmd, cwd=repo_path, tool="npm_test", timeout_s=timeout_s)


_PASS_WITH_NO_TESTS_RUNNERS = ("jest", "vitest", "playwright", "bun")


def _node_runner_supports_pass_with_no_tests(repo_path: Path) -> bool:
    try:
        import json
        pkg = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    test_script = (pkg.get("scripts") or {}).get("test", "").lower()
    return any(runner in test_script for runner in _PASS_WITH_NO_TESTS_RUNNERS)


def _run_go(repo_path: Path, timeout_s: int) -> ValidationResult:
    if shutil.which("go") is None:
        return _skipped("go not installed")
    cmd = ["go", "test", "./..."]
    return _exec(cmd, cwd=repo_path, tool="go_test", timeout_s=timeout_s)


def _run_rust(repo_path: Path, timeout_s: int) -> ValidationResult:
    if shutil.which("cargo") is None:
        return _skipped("cargo not installed")
    cmd = ["cargo", "test"]
    return _exec(cmd, cwd=repo_path, tool="cargo_test", timeout_s=timeout_s)


def _run_lint_only_python(repo_path: Path, timeout_s: int) -> ValidationResult:
    """Lightweight fallback: ruff → flake8 → nothing."""
    for linter in ("ruff", "flake8"):
        if shutil.which(linter) is None:
            continue
        cmd = [linter, str(repo_path)] if linter == "flake8" else [linter, "check", str(repo_path)]
        return _exec(cmd, cwd=repo_path, tool="lint_only", timeout_s=timeout_s)
    return _skipped("no Python test runner (pytest/unittest) or linter (ruff/flake8) found")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _exec(
    cmd: list[str],
    cwd: Path,
    tool: str,
    timeout_s: int,
) -> ValidationResult:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        combined = (proc.stdout + proc.stderr)[-_OUTPUT_LIMIT:]
        passed = proc.returncode == 0
        errors: list[str] = []
        if not passed:
            # Extract the tail of stderr as a concise error
            tail = proc.stderr.strip()[-1000:]
            if tail:
                errors.append(tail)
        return ValidationResult(
            passed=passed,
            tool=tool,
            duration_ms=elapsed,
            errors=errors,
            raw_output=combined,
        )
    except subprocess.TimeoutExpired:
        elapsed = int((time.monotonic() - t0) * 1000)
        return ValidationResult(
            passed=False,
            tool=tool,
            duration_ms=elapsed,
            errors=[f"Validation timed out after {timeout_s}s"],
            raw_output="",
        )
    except OSError as exc:
        return ValidationResult(
            passed=False,
            tool=tool,
            duration_ms=0,
            errors=[str(exc)],
            raw_output="",
        )


def _detect_language(repo_path: Path) -> str | None:
    if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
        return "python"
    if (repo_path / "package.json").exists():
        return "node"
    if (repo_path / "go.mod").exists():
        return "go"
    if (repo_path / "Cargo.toml").exists():
        return "rust"
    # Fallback: presence of Python files alone
    if any(repo_path.rglob("*.py")):
        return "python"
    return None


def suggest_test_commands(repo_path: Path, file_path: str = "") -> tuple[str, list[str]]:
    """Return ``(tool, commands)`` agents should run to verify a fix.

    Does not execute tests — only suggests commands based on project type.
    """
    lang = _detect_language(repo_path)
    rel = (file_path or "").lstrip("./")
    if lang == "python":
        cmds = ["pytest -x --tb=short -q"]
        if rel.endswith(".py"):
            cmds.insert(0, f"pytest {rel} -q --tb=short")
        return "pytest", cmds
    if lang == "node":
        return "npm_test", ["npm test -- --passWithNoTests"]
    if lang == "go":
        cmds = ["go test ./..."]
        if rel.endswith(".go"):
            parent = str(Path(rel).parent).replace("\\", "/")
            if parent and parent != ".":
                cmds.insert(0, f"go test ./{parent}/...")
        return "go_test", cmds
    if lang == "rust":
        return "cargo_test", ["cargo test"]
    return "skipped", []


def _find_python_runner(repo_path: Path) -> list[str] | None:
    """Return the pytest invocation to use, or None if no runner is found."""
    # Prefer the venv's pytest
    for venv_dir in (".venv", "venv", ".env", "env"):
        venv_pytest = repo_path / venv_dir / "bin" / "pytest"
        if venv_pytest.is_file():
            return [str(venv_pytest)]
    # System pytest
    if shutil.which("pytest"):
        return ["pytest"]
    # python -m pytest
    python_bin = shutil.which("python3") or shutil.which("python")
    if python_bin:
        result = subprocess.run(
            [python_bin, "-m", "pytest", "--version"],
            capture_output=True,
        )
        if result.returncode == 0:
            return [python_bin, "-m", "pytest"]
    return None


def _is_writable(path: Path) -> bool:
    probe = path / ".ai_audit_write_probe"
    try:
        probe.touch()
        probe.unlink()
        return True
    except (PermissionError, OSError):
        return False


def _skipped(reason: str) -> ValidationResult:
    """A skipped run is *not* a passed run — callers must treat
    ``skipped_reason`` as a third state distinct from pass/fail.
    Returning ``passed=True`` previously caused the repair loop to accept
    unvalidated patches whenever pytest was missing or the mount was read-only.
    """
    return ValidationResult(
        passed=False,
        tool="skipped",
        duration_ms=0,
        errors=[],
        raw_output="",
        skipped_reason=reason,
    )


# ── Failure summary (LLM-friendly compact output) ────────────────────────────

def extract_failure_summary(raw_output: str, tool: str) -> list[str]:
    """Extract the most actionable failure lines from test runner output.

    Returns at most ``_SUMMARY_LINE_LIMIT`` lines, chosen to be maximally
    informative for a local LLM with a limited context window.  Strips
    boilerplate (progress bars, timing lines, PASSED lines) and keeps only
    failed-test names and short error messages.
    """
    if not raw_output:
        return []

    lines = raw_output.splitlines()

    if tool in ("pytest", "lint_only"):
        result = _pytest_summary(lines)
    elif tool == "go_test":
        result = _go_summary(lines)
    elif tool == "cargo_test":
        result = _cargo_summary(lines)
    elif tool == "npm_test":
        result = _npm_summary(lines)
    else:
        result = []

    # Universal fallback: last N non-empty, non-boilerplate lines
    if not result:
        result = [l for l in lines if l.strip() and not _is_boilerplate(l)][-10:]

    return result[:_SUMMARY_LINE_LIMIT]


def _is_boilerplate(line: str) -> bool:
    """True for lines that carry no signal for the LLM."""
    stripped = line.strip()
    if not stripped:
        return True
    # pytest progress dots / timing / separator lines
    if re.match(r"^[.FEs ]+\s*\[?\s*\d+%\]?", stripped):
        return True
    if re.match(r"^={5,}|^-{5,}", stripped):
        return True
    if re.match(r"^\d+ passed", stripped):
        return True
    return False


def _pytest_summary(lines: list[str]) -> list[str]:
    out: list[str] = []

    # FAILED / ERROR test lines (e.g. "FAILED tests/test_foo.py::test_bar - AssertionError")
    for l in lines:
        if l.startswith(("FAILED ", "ERROR ")):
            out.append(l)

    # E-prefixed assertion/exception lines from tracebacks (e.g. "E   AssertionError: …")
    for l in lines:
        if re.match(r"^E\s+\S", l):
            out.append(l)

    # ruff / flake8 lint output — lines with a file:line:col pattern
    if not out:
        for l in lines:
            if re.match(r"^\S.*:\d+:\d+:\s+[A-Z]\d+", l):
                out.append(l)

    return out


def _go_summary(lines: list[str]) -> list[str]:
    out: list[str] = []
    for l in lines:
        if l.startswith(("--- FAIL:", "FAIL\t", "panic:", "got ")):
            out.append(l)
        elif re.search(r"\bError\b|\bfailed\b", l, re.IGNORECASE):
            out.append(l)
    return out


def _cargo_summary(lines: list[str]) -> list[str]:
    out: list[str] = []
    for l in lines:
        if re.search(r"^FAILED|^error\[|panicked at|thread '.*' panicked", l):
            out.append(l)
    return out


def _npm_summary(lines: list[str]) -> list[str]:
    out: list[str] = []
    for l in lines:
        if re.search(r"(✕|●\s+\S|FAIL\s|\bExpected:\b|\bReceived:\b|Error:)", l):
            out.append(l)
    return out


# ── Patch validation (per project, clean tree) ───────────────────────────────
#
# validate_patch applies a patch to a clean tree of the repo and validates the projects it touches.
# A repo can hold several Python projects (agent-composer: rag-chat/, rag-ingest/, … each with
# its own requirements and tests), so each changed .py file maps to its nearest project root and
# that project's tests run in a venv built from its declared dependencies, cached by their hash.
# The package itself is never installed: tests import the patched tree via PYTHONPATH.

_PROJECT_MARKERS = ("pyproject.toml", "setup.py", "requirements.txt")
_REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt", "requirements-test.txt")
_PYPROJECT_EXTRAS = ("dev", "test", "tests")
# Fatal-only lint (syntax errors, undefined names, bad comparisons): what flake8 calls the "E9,F63,F7,F82"
# set. Style rules are left out on purpose, and --isolated ignores repo config that would add them.
_RUFF_SELECT = "E9,F63,F7,F82"
VENV_BUILD_TIMEOUT_S = 900
_ENV_GAP_RE = re.compile(r"\b(ModuleNotFoundError|ImportError)\b")
_RESULT_COUNTS_RE = re.compile(r"\b\d+ (passed|failed)\b")
_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


def project_root(tree: Path, rel_path: str) -> Path:
    """Nearest ancestor of *rel_path* inside *tree* that looks like a Python project, else *tree*."""
    p = (tree / rel_path).parent
    tree = tree.resolve()
    while True:
        try:
            p.resolve().relative_to(tree)
        except ValueError:
            return tree
        if any((p / m).exists() for m in _PROJECT_MARKERS) or (p / "tests").is_dir():
            return p
        if p.resolve() == tree:
            return tree
        p = p.parent


def _declared_deps(root: Path) -> tuple[list[Path], list[str]]:
    """Requirement files and pyproject dependency specs (project + dev/test extras) of *root*."""
    req_files = [root / n for n in _REQUIREMENT_FILES if (root / n).is_file()]
    specs: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        try:
            project = (tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project") or {})
        except (OSError, ValueError):
            project = {}
        specs += [str(s) for s in project.get("dependencies") or []]
        extras = project.get("optional-dependencies") or {}
        for name in _PYPROJECT_EXTRAS:
            specs += [str(s) for s in extras.get(name) or []]
    return req_files, specs


def _requirement_lines(path: Path, depth: int = 0) -> list[str]:
    """Lines of a requirements file with ``-r`` includes inlined (relative to the file).

    Lines that install the project itself (``.``, ``-e .``) and constraint files are dropped."""
    out: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for ln in text.splitlines():
        s = ln.split(" #", 1)[0].strip()
        if not s or s.startswith("#") or s in (".", "-e .", "-e.") or s.startswith(("-c ", "--constraint")):
            continue
        m = re.match(r"^(?:-r|--requirement)[ =]?\s*(\S+)$", s)
        if m:
            if depth < 5:
                out += _requirement_lines(path.parent / m.group(1), depth + 1)
            continue
        out.append(s)
    return out


def ensure_test_venv(root: Path, cache_dir: Path) -> tuple[Path | None, str]:
    """Python of a venv holding *root*'s declared deps plus pytest; built once per deps hash.

    Returns ``(python, "")`` or ``(None, error)``. Requirement lines that install the project
    itself (``-e .``, ``.``) are skipped: the venv outlives this tree, and tests import the
    patched code through PYTHONPATH instead."""
    import fcntl
    import hashlib
    import sys

    req_files, specs = _declared_deps(root)
    lines: list[str] = []
    for f in req_files:
        lines += _requirement_lines(f)
    lines = list(dict.fromkeys(lines + specs))
    key = hashlib.sha256("\n".join([sys.version, *sorted(lines)]).encode()).hexdigest()[:16]
    cache_dir = cache_dir.resolve()  # tests run from the project dir, so a relative path would break
    cache_dir.mkdir(parents=True, exist_ok=True)
    venv = cache_dir / f"{root.name or 'repo'}-{key}"
    python = venv / "bin" / "python"
    with open(cache_dir / f".{venv.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one build per env; others wait, then reuse it
        if (venv / ".ready").exists():
            return python, ""
        shutil.rmtree(venv, ignore_errors=True)
        reqs = cache_dir / f".{venv.name}.txt"
        reqs.write_text("\n".join(["pytest", *lines]) + "\n", encoding="utf-8")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True,
                           text=True, timeout=120)
            subprocess.run([str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "-r", str(reqs)], check=True, capture_output=True, text=True,
                           timeout=VENV_BUILD_TIMEOUT_S)
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(venv, ignore_errors=True)
            return None, ((exc.stderr or exc.stdout or "").strip()[-800:] or str(exc))
        except (subprocess.TimeoutExpired, OSError) as exc:
            shutil.rmtree(venv, ignore_errors=True)
            return None, str(exc)
        (venv / ".ready").write_text(key, encoding="utf-8")
        logger.info("built test venv %s for %s", venv.name, root)
        return python, ""


def _lint_files(tree: Path, files: list[str], timeout_s: int) -> ValidationResult:
    """Fatal-only lint of the changed files: ruff when installed, else a compile check."""
    if shutil.which("ruff"):
        return _exec(["ruff", "check", "--isolated", "--no-cache", "--select", _RUFF_SELECT, *files],
                     cwd=tree, tool="lint_only", timeout_s=timeout_s)
    import sys

    return _exec([sys.executable, "-m", "py_compile", *files], cwd=tree, tool="lint_only", timeout_s=timeout_s)


def _run_project_tests(root: Path, python: Path, timeout_s: int) -> tuple[str, ValidationResult]:
    """Run *root*'s tests. Returns ``(outcome, result)``: ``ran``, ``no_tests`` or ``env_gap``.

    ``env_gap``: collection failed on a missing module, so the env, not the patch, is the
    problem (a dependency the project doesn't declare, such as torch from a base image)."""
    import os

    paths = [str(root / "src")] if (root / "src").is_dir() else []
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([*paths, str(root)]), "PYTHONDONTWRITEBYTECODE": "1"}
    t0 = time.monotonic()
    try:
        proc = subprocess.run([str(python), "-m", "pytest", "-q", "--tb=short", "-rfE", "-p", "no:cacheprovider"],
                              cwd=root, env=env, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return "ran", ValidationResult(passed=False, tool="pytest", duration_ms=int((time.monotonic() - t0) * 1000),
                                       errors=[f"tests timed out after {timeout_s}s"], raw_output="")
    except OSError as exc:  # the env's interpreter is gone or broken: an env problem, not the patch's
        return "env_gap", ValidationResult(passed=False, tool="pytest", duration_ms=0, errors=[str(exc)],
                                           raw_output=f"ImportError: cannot run tests: {exc}")
    out = (proc.stdout + proc.stderr)[-_OUTPUT_LIMIT:]
    res = ValidationResult(passed=proc.returncode == 0, tool="pytest",
                           duration_ms=int((time.monotonic() - t0) * 1000),
                           errors=[] if proc.returncode == 0 else [proc.stderr.strip()[-1000:] or out[-1000:]],
                           raw_output=out)
    if proc.returncode == 5:  # pytest: no tests collected
        return "no_tests", res
    # only collection errors, at least one from a missing module, and no test ran at all
    if proc.returncode != 0 and _ENV_GAP_RE.search(out) and not _RESULT_COUNTS_RE.search(out):
        return "env_gap", res
    return "ran", res


def _failed_ids(output: str) -> set[str]:
    return set(_FAILED_ID_RE.findall(output))


def _has_project(root: Path) -> bool:
    return any((root / m).exists() for m in _PROJECT_MARKERS) or (root / "tests").is_dir()


def validate_patch_tree(
    repo: RepoConfig, tree: Path, changed: list[str], *, timeout_s: int = 120, cache_dir: Path | None = None,
    base_tree: "Callable[[], Path | None] | None" = None,
) -> ValidationResult:
    """Validate a patched clean *tree*: lint the changed .py files, then test each touched project.

    Passes when lint passes and every project whose tests ran passed (tool ``pytest``); when no
    tests could run (none exist, the env failed to build, or a module was missing), the lint
    result stands (tool ``lint_only``) with a note saying why. Patches without Python changes
    go to :func:`validate_repo` for the repo's own language.

    *base_tree* returns the same tree without the patch (built on demand). When a project's tests
    fail, they run there too, and only failures the patch introduced count: a test already
    failing on the base branch is named in a note but doesn't fail the patch."""
    py = sorted({f for f in changed if f.endswith(".py") and (tree / f).is_file()})
    if not py:
        return validate_repo(repo, path_override=tree, timeout_s=timeout_s)
    lint = _lint_files(tree, py, timeout_s)
    if not lint.passed:
        return lint
    cache = cache_dir or Path(".validate_venvs")
    notes: list[str] = []
    ran: list[tuple[str, ValidationResult]] = []
    for root in sorted({project_root(tree, f) for f in py}):
        label = str(root.relative_to(tree.resolve())) if root != tree.resolve() else "."
        if not _has_project(root):
            notes.append(f"{label}: no project (pyproject, setup.py, requirements or tests/) for "
                         f"{', '.join(f for f in py if project_root(tree, f) == root)}; lint only")
            continue
        python, err = ensure_test_venv(root, cache)
        if python is None:
            notes.append(f"{label}: test env did not build, lint only: {err[:300]}")
            continue
        outcome, res = _run_project_tests(root, python, timeout_s)
        if outcome == "no_tests":
            notes.append(f"{label}: no tests collected, lint only")
        elif outcome == "env_gap":
            m = _ENV_GAP_RE.search(res.raw_output)
            line = next((ln for ln in res.raw_output.splitlines() if m and m.group(1) in ln), "")
            notes.append(f"{label}: tests need a module the env lacks, lint only: {line.strip()[:200]}")
        elif not res.passed and base_tree is not None:
            res = _against_base(root, tree, base_tree, python, timeout_s, res, label, notes)
            ran.append((label, res))
        else:
            ran.append((label, res))
    note = "\n".join(notes)
    failed = [(lbl, r) for lbl, r in ran if not r.passed]
    if failed:
        return ValidationResult(
            passed=False, tool="pytest", duration_ms=lint.duration_ms + sum(r.duration_ms for _, r in ran),
            errors=[e for _, r in failed for e in r.errors],
            raw_output="\n".join(f"== {lbl}\n{r.raw_output}" for lbl, r in failed)[-_OUTPUT_LIMIT:],
        )
    head = "tests passed: " + ", ".join(lbl for lbl, _ in ran) if ran else "lint passed; no tests ran"
    if notes and ran:
        head += " (see notes)"
    return ValidationResult(
        passed=True, tool="pytest" if ran else "lint_only",
        duration_ms=lint.duration_ms + sum(r.duration_ms for _, r in ran), errors=[],
        raw_output="\n".join(x for x in (head, note) if x)[-_OUTPUT_LIMIT:],
    )


def _against_base(
    root: Path, tree: Path, base_tree: "Callable[[], Path | None]", python: Path, timeout_s: int,
    res: ValidationResult, label: str, notes: list[str],
) -> ValidationResult:
    """Re-judge a failing run by what the patch broke: failures also present without it don't count."""
    patched = _failed_ids(res.raw_output)
    base = base_tree()
    if base is None or not patched:
        return res
    base_root = base / root.relative_to(tree.resolve())
    if not base_root.is_dir():
        return res
    outcome, base_res = _run_project_tests(base_root, python, timeout_s)
    if outcome != "ran":
        return res
    already = _failed_ids(base_res.raw_output)
    new = sorted(patched - already)
    if new:
        res.raw_output = (f"new failures from this patch: {', '.join(new)}\n" + res.raw_output)[-_OUTPUT_LIMIT:]
        return res
    notes.append(f"{label}: {len(patched)} test(s) fail on the base branch too, not counted: "
                 f"{', '.join(sorted(patched))[:400]}")
    return ValidationResult(passed=True, tool="pytest", duration_ms=res.duration_ms + base_res.duration_ms,
                            errors=[], raw_output=res.raw_output)
