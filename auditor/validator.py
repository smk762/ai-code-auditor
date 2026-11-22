"""Per-repo validation runner: detect language, run test suite, return ValidationResult.

Designed to be called both on live repo paths and on temporary copies (used by the
repair loop to test a patch before surfacing it to the user).
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from auditor.config import RepoConfig
from auditor.contracts import ValidationResult
from auditor.repo_resolver import resolve_repo_path
from auditor.proc import child_env, run as proc_run
from auditor import validate_cache

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
    test_script = _table(_table(pkg).get("scripts")).get("test")
    if not isinstance(test_script, str):
        return False
    return any(runner in test_script.lower() for runner in _PASS_WITH_NO_TESTS_RUNNERS)


def _table(value: object) -> dict:
    """*value* when it is a mapping, else {}: parsed JSON/TOML from an audited repo is untrusted,
    and a key that should hold a table can hold a string, number or list."""
    return value if isinstance(value, dict) else {}


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
        proc = proc_run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # a tool echoing non-UTF-8 bytes must not crash the run
            timeout=timeout_s,
            env=child_env(),
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        combined = (proc.stdout + proc.stderr)[-_OUTPUT_LIMIT:]
        passed = proc.returncode == 0
        errors: list[str] = []
        if not passed:
            # The tail of stderr as a concise error; stdout's when stderr is empty, since ruff,
            # pytest and most linters report there.
            tail = proc.stderr.strip()[-1000:] or proc.stdout.strip()[-1000:]
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

    Does not execute tests — only suggests commands based on project type. Paths come from
    the audited (untrusted) repo, so every one is shell-quoted.
    """
    rel = file_path or ""
    while rel.startswith("./"):  # a literal "./" prefix only: lstrip("./") ate ".github/" and "../"
        rel = rel[2:]
    from auditor import validate_node

    if rel.endswith(validate_node.CODE_EXTS):
        root = validate_node.node_root(repo_path, rel)
        checks = validate_node.project_checks(root) if root is not None else []
        if checks:
            sub = root.relative_to(repo_path.resolve()).as_posix()
            prefix = f"cd {shlex.quote(sub)} && " if sub != "." else ""
            return validate_node.TOOL, [prefix + "npm ci", *(prefix + "npm " + " ".join(a) for _, a in checks)]
    lang = _detect_language(repo_path)
    if lang == "python":
        cmds = ["pytest -x --tb=short -q"]
        if rel.endswith(".py"):
            cmds.insert(0, f"pytest {shlex.quote(rel)} -q --tb=short")
        return "pytest", cmds
    if lang == "node":
        return "npm_test", ["npm test -- --passWithNoTests"]
    if lang == "go":
        cmds = ["go test ./..."]
        if rel.endswith(".go"):
            parent = str(Path(rel).parent).replace("\\", "/")
            if parent and parent != ".":
                cmds.insert(0, f"go test {shlex.quote(f'./{parent}/...')}")
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
    """Whether a file can be created in *path*. The probe has a unique name (``O_EXCL``), so
    concurrent checks don't remove each other's probes and an existing file is never touched."""
    import tempfile

    try:
        fd, name = tempfile.mkstemp(dir=path, prefix=".ai_audit_write_probe.")
    except OSError:
        return False
    os.close(fd)
    Path(name).unlink(missing_ok=True)
    return True


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

    result: list[str] = []
    for part in tool.split("+"):  # a composite tool ("pytest+npm_checks") reads each part's own way
        summarize = _SUMMARIZERS.get(part)
        if summarize is not None:
            result += summarize(lines)
    result = list(dict.fromkeys(result))

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
    # only a bare rule: "==== FAILURES ====" or "==== 2 failed, 3 passed in 1s ====" carry the verdict
    if re.fullmatch(r"[=\-]{5,}", stripped):
        return True
    # a fully clean count line; one that also reports errors, warnings or xpasses is kept
    if re.fullmatch(r"=*\s*\d+ passed(?:, \d+ (?:skipped|deselected|xfailed))*(?: in [\d.]+s(?: \([^)]*\))?)?\s*=*",
                    stripped):
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


def _node_checks_summary(lines: list[str]) -> list[str]:
    """tsc diagnostics, eslint errors, vitest failures and the ``-- <project> npm run <script>`` headers."""
    out: list[str] = []
    for l in lines:
        if re.search(r"(^-- \S+ npm run |^new \S+ errors from this patch|\): error TS\d+|^\s+\d+:\d+\s+error\s|"
                     r"^\s*FAIL\s|AssertionError|SyntaxError)", l):
            out.append(l.rstrip())
    return out


def _npm_summary(lines: list[str]) -> list[str]:
    out: list[str] = []
    for l in lines:
        if re.search(r"(✕|●\s+\S|FAIL\s|\bExpected:|\bReceived:|Error:)", l):
            out.append(l)
    return out


_SUMMARIZERS: dict[str, Callable[[list[str]], list[str]]] = {
    "pytest": _pytest_summary, "lint_only": _pytest_summary, "go_test": _go_summary,
    "cargo_test": _cargo_summary, "npm_test": _npm_summary,
    "npm_checks": _node_checks_summary, "node_check": _node_checks_summary,
}


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
# argus-ci's python-ci.yml installs `.[<test-extras>]` before pytest; a repo's CI names the extras
# its tests need (argus-forge: "dev,server,cli"), so the venv installs the same ones.
_CI_TEST_EXTRAS_RE = re.compile(r"^\s*test-extras:\s*[\"']?([A-Za-z0-9_,\s-]+?)[\"']?\s*(?:#.*)?$", re.MULTILINE)
# Standalone workflows install extras themselves: `uv sync --all-extras` (argus-mint, atlas, survey)
# or `pip install -e ".[server,dev]"` / `uv pip install ".[dev,cli]"` (realms, quarry).
_CI_INSTALL_EXTRAS_RE = re.compile(r"\binstall\b[^\n]*?[\"']?\.\[([A-Za-z0-9_,\s-]+)\]")
_CI_ALL_EXTRAS_RE = re.compile(r"--all-extras\b")
ALL_EXTRAS = "*"  # in ci_test_extras(): CI installs every extra the project declares
# Fatal-only lint (syntax errors, undefined names, bad comparisons): what flake8 calls the "E9,F63,F7,F82"
# set. Style rules are left out on purpose, and --isolated ignores repo config that would add them.
_RUFF_SELECT = "E9,F63,F7,F82"
VENV_BUILD_TIMEOUT_S = 900
_ENV_GAP_RE = re.compile(r"\b(ModuleNotFoundError|ImportError)\b")
_RESULT_COUNTS_RE = re.compile(r"\b\d+ (passed|failed)\b")
_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
_ERRORED_ID_RE = re.compile(r"^ERROR (\S+)", re.MULTILINE)


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
            return p.resolve()  # resolved, like *tree*: callers take relative_to(tree.resolve())
        if p.resolve() == tree:
            return tree
        p = p.parent


def ci_test_extras(root: Path) -> list[str]:
    """Extras the repo's CI installs for its tests, from the nearest ``.github/workflows`` at or
    above *root*: argus-ci's ``test-extras`` input, extras a step installs (``.[a,b]``), and
    :data:`ALL_EXTRAS` for ``--all-extras``. [] when none is set."""
    found: list[str] = []
    for text in _workflow_texts(root):
        for m in [*_CI_TEST_EXTRAS_RE.finditer(text), *_CI_INSTALL_EXTRAS_RE.finditer(text)]:
            found += [e.strip() for e in m.group(1).split(",") if e.strip()]
        if _CI_ALL_EXTRAS_RE.search(text):
            found.append(ALL_EXTRAS)
    return list(dict.fromkeys(found))


# Loose tests (#102): a repo with no project marker (argus-ci) keeps test_*.py next to the code it
# tests. Its CI installs what they need inline: `uv run --with pyyaml ...`, `pip install pyyaml`.
_LOOSE_TEST_RE = re.compile(r"^(test_[^/]*|[^/]*_test)\.py$")
_CI_WITH_RE = re.compile(r"--with[ =]([^\s'\"]+)")
_CI_PIP_INSTALL_RE = re.compile(r"\bpip3? install\b([^\n#;&|]*)")  # one command: stop at ; && ||
# pip install options whose value is a separate token (`--target vendor`): the value isn't a package
_PIP_VALUE_OPTS = frozenset({
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url", "--extra-index-url",
    "-f", "--find-links", "-t", "--target", "--prefix", "--root", "--platform", "--python-version",
    "--implementation", "--abi", "--only-binary", "--no-binary", "--src", "--upgrade-strategy", "--progress-bar",
    "--trusted-host", "--proxy", "--cert", "--client-cert", "--cache-dir", "--log", "--timeout", "--retries",
    "--exists-action", "--report", "--python", "-C", "--config-settings", "--global-option", "--build-option",
    "--keyring-provider", "--root-user-action", "--use-feature", "--use-deprecated", "--group",
})
_PLAIN_REQ_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,-]+\])?(?:[<>=!~]=?[A-Za-z0-9.*+!-]+)?$")
# Third-party modules loose tests commonly import whose distribution name differs or is obvious.
# A fixed list, never the import name itself: a patch's imports must not pick what pip installs.
_IMPORT_DISTS = {"yaml": "pyyaml", "toml": "toml", "tomli": "tomli", "requests": "requests",
                 "jsonschema": "jsonschema", "httpx": "httpx"}
_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def _workflow_texts(root: Path) -> list[str]:
    """Texts of the workflows in the nearest ``.github/workflows`` at or above *root*, within its repo."""
    for d in (root, *root.parents[:4]):  # a project dir is at most a few levels into its repo
        wf = d / ".github" / "workflows"
        if wf.is_dir():
            out = []
            for f in sorted([*wf.glob("*.yml"), *wf.glob("*.yaml")]):
                try:
                    out.append(f.read_text(encoding="utf-8", errors="replace"))  # a stray non-UTF-8 byte
                except OSError:
                    continue
            return out
        if (d / ".git").exists():
            break
    return []


def ci_tool_deps(root: Path) -> list[str]:
    """Packages the repo's CI installs by name (``--with a,b``, ``pip install a b``), for a repo
    with no declared dependencies. Paths, editable installs, options and expressions are skipped."""
    found: list[str] = []
    for text in _workflow_texts(root):
        for m in _CI_WITH_RE.finditer(text):
            found += m.group(1).split(",")
        for m in _CI_PIP_INSTALL_RE.finditer(text):
            if "$" in m.group(1):  # an expression (${{ inputs.x }}, $EXTRAS): what it installs is unknown
                continue
            toks = [t.strip("'\"") for t in m.group(1).split()]
            skip = False
            for t in toks:
                if skip:
                    skip = False
                    continue
                if t in _PIP_VALUE_OPTS:  # its value is the next token, never a package
                    skip = True
                    continue
                found.append(t)
    return list(dict.fromkeys(t.strip() for t in found if _PLAIN_REQ_RE.match(t.strip())))


def loose_tests(root: Path, files: list[str]) -> dict[str, list[str]]:
    """Test files to run for *files* (paths relative to *root*) in a root with no project marker:
    every ``test_*.py`` / ``*_test.py`` in each changed file's directory, grouped by that directory."""
    groups: dict[str, list[str]] = {}
    for d in sorted({str(Path(f).parent) for f in files}):
        dir_ = root / d
        tests = sorted(str((dir_ / n.name).relative_to(root)) for n in dir_.glob("*.py")
                       if _LOOSE_TEST_RE.match(n.name) and n.is_file()) if dir_.is_dir() else []
        if tests:
            groups[d] = tests
    return groups


def loose_test_deps(root: Path, tests: list[str]) -> list[str]:
    """What a markerless root's tests need: the packages its CI installs by name, plus the
    distributions of known third-party modules the test files import (:data:`_IMPORT_DISTS`)."""
    deps = ci_tool_deps(root)
    have = {_dist_name(d) for d in deps}
    for t in tests:
        try:
            text = (root / t).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for mod in _IMPORT_RE.findall(text):
            dist = _IMPORT_DISTS.get(mod)
            if dist and dist not in have:  # a CI pin of the same package wins
                have.add(dist)
                deps.append(dist)
    return deps


def _dist_name(spec: str) -> str:
    m = _REQ_NAME.match(spec)
    return _norm_name(m.group(1)) if m else spec


def _declared_deps(root: Path) -> tuple[list[Path], list[str]]:
    """Requirement files and pyproject dependency specs of *root*: the project's dependencies
    plus its dev/test extras and the extras its CI installs for tests (:func:`ci_test_extras`)."""
    req_files = [root / n for n in _REQUIREMENT_FILES if (root / n).is_file()]
    specs: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        try:
            project = (tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project") or {})
        except (OSError, ValueError):
            project = {}
        extras = project.get("optional-dependencies") or {}
        own = _norm_name(str(project.get("name") or ""))
        specs += _expand_self_refs([str(s) for s in project.get("dependencies") or []], own, extras)
        names = [*_PYPROJECT_EXTRAS, *ci_test_extras(root)]
        if ALL_EXTRAS in names:
            names = [*_PYPROJECT_EXTRAS, *extras]
        for name in dict.fromkeys(n for n in names if n != ALL_EXTRAS):
            specs += _expand_self_refs([str(s) for s in extras.get(name) or []], own, extras, frozenset({name}))
    return req_files, list(dict.fromkeys(specs))


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")


def _norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _expand_self_refs(specs: list[str], own: str, extras: dict, seen: frozenset[str] = frozenset()) -> list[str]:
    """Replace ``<this project>[a,b]`` with extras a and b's own specs, recursively.

    A ``dev = ["pkg[server,cli]", ...]`` extra names the project itself. Passed to pip as is, it
    installs the *published* release from PyPI, whose pins conflict with the patched tree's
    (argus-mask 0.2.0 pinned argus-cortex<0.6 while dev asked for >=0.9: no venv, lint only)."""
    out: list[str] = []
    for spec in specs:
        m = _REQ_NAME.match(spec)
        if not own or not m or _norm_name(m.group(1)) != own:
            out.append(spec)
            continue
        for extra in (e.strip() for e in (m.group(2) or "").split(",") if e.strip()):
            if extra not in seen:
                out += _expand_self_refs([str(s) for s in extras.get(extra) or []], own, extras, seen | {extra})
    return out


def _repo_boundary(path: Path) -> Path:
    """The repository holding *path*: the nearest ancestor with a ``.git``, else the file's dir."""
    start = path.resolve().parent
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def _requirement_lines(path: Path, depth: int = 0, root: Path | None = None,
                       _seen: set[Path] | None = None) -> list[str]:
    """Lines of a requirements file with ``-r`` includes inlined (relative to the file).

    Lines that install the project itself (``.``, ``-e .``) and constraint files are dropped.
    An include that is absolute or resolves outside the repository (*root*, by default the
    repo holding *path*) is skipped: the audited repo must not make us read host files.
    Each file is read once: a self-include or a cycle would otherwise cost N^depth reads."""
    root = (root or _repo_boundary(path)).resolve()
    seen = set() if _seen is None else _seen
    seen.add(path.resolve())
    out: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for ln in _logical_lines(text):
        s = _HASH_OPT_RE.sub("", ln.split(" #", 1)[0]).strip()
        if not s or s.startswith("#") or s in (".", "-e .", "-e.") or s.startswith(("-c ", "--constraint")):
            continue
        m = re.match(r"^(?:-r|--requirement)[ =]?\s*(\S+)$", s)
        if m:
            inc = m.group(1)
            target = (path.parent / inc).resolve()
            if Path(inc).is_absolute() or not target.is_relative_to(root):
                logger.warning("skipping requirements include outside the repo: %s (in %s)", inc, path.name)
                continue
            if depth < 5 and target not in seen:
                out += _requirement_lines(target, depth + 1, root, seen)
            continue
        out.append(s)
    return out


_HASH_OPT_RE = re.compile(r"(?:^|\s)--hash[= ]\S+")


def _logical_lines(text: str) -> list[str]:
    """Lines of a requirements file with backslash continuations joined, as pip reads them
    (pip-compile --generate-hashes writes ``pkg==1.0 \\`` then indented ``--hash=...`` lines)."""
    out: list[str] = []
    buf = ""
    for ln in text.splitlines():
        if ln.rstrip().endswith("\\") and not ln.lstrip().startswith("#"):
            buf += ln.rstrip()[:-1] + " "
            continue
        out.append(buf + ln)
        buf = ""
    if buf:
        out.append(buf)
    return out


# Minor versions a project's requires-python may pick when the image's interpreter doesn't satisfy it.
TEST_PYTHONS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14")


def requires_python(root: Path) -> str:
    """``[project] requires-python`` of *root*, or ""."""
    import tomllib

    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project") or {}
    except (OSError, ValueError):
        return ""
    return str(project.get("requires-python") or "").strip()


def test_python(root: Path, here: str = "") -> tuple[str, str]:
    """``(minor, "")`` for the interpreter *root*'s tests should run on, or ``("", error)``.

    The image's own interpreter when it satisfies the project's ``requires-python`` (or the project
    declares none): nothing to download, and envs keep their cache keys. Otherwise the oldest
    minor version that satisfies it, which is what a project's CI most often tests first (#141)."""
    import sys

    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    here = here or f"{sys.version_info[0]}.{sys.version_info[1]}"
    spec_text = requires_python(root)
    if not spec_text:
        return here, ""
    try:
        spec = SpecifierSet(spec_text)
    except InvalidSpecifier:
        return here, ""  # an unreadable spec: keep the old behaviour rather than refuse

    def fits(minor: str) -> bool:
        return any(spec.contains(f"{minor}.{patch}") for patch in (0, 99))

    if fits(here):
        return here, ""
    for minor in TEST_PYTHONS:
        if fits(minor):
            return minor, ""
    return "", f"no supported Python satisfies requires-python {spec_text!r} (tried {', '.join(TEST_PYTHONS)})"


def _uv() -> str:
    import sys

    return shutil.which("uv") or str(Path(sys.executable).with_name("uv"))


def venv_python_version(python: Path) -> str:
    """The ``major.minor.patch`` a venv's interpreter is, from its ``pyvenv.cfg``, or ""."""
    try:
        text = (python.parent.parent / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^version(?:_info)?\s*=\s*([0-9][0-9.]*)", text, re.M)
    return m.group(1) if m else ""


def ensure_test_venv(root: Path, cache_dir: Path, extra: list[str] | None = None) -> tuple[Path | None, str]:
    """Python of a venv holding *root*'s declared deps plus pytest; built once per deps hash.

    Returns ``(python, "")`` or ``(None, error)``. Requirement lines that install the project
    itself (``-e .``, ``.``) are skipped: the venv outlives this tree, and tests import the
    patched code through PYTHONPATH instead. *extra* adds specs (a markerless root's
    :func:`loose_test_deps`). The interpreter is :func:`test_python`'s: the image's own, or one uv
    provides when the project's ``requires-python`` rules the image's out."""
    import fcntl
    import hashlib
    import sys

    req_files, specs = _declared_deps(root)
    lines: list[str] = []
    for f in req_files:
        lines += _requirement_lines(f)
    lines = list(dict.fromkeys(lines + specs + list(extra or [])))
    minor, why = test_python(root)
    if not minor:
        return None, why
    here = f"{sys.version_info[0]}.{sys.version_info[1]}"
    # the image's interpreter keys on its full version, as before; a uv one on the minor it asked for
    interp = sys.version if minor == here else f"uv-python-{minor}"
    key = hashlib.sha256("\n".join([interp, *sorted(lines)]).encode()).hexdigest()[:16]
    cache_dir = cache_dir.resolve()  # tests run from the project dir, so a relative path would break
    cache_dir.mkdir(parents=True, exist_ok=True)
    venv = cache_dir / f"{root.name or 'repo'}-{key}"
    python = venv / "bin" / "python"
    with open(cache_dir / f".{venv.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one build per env; others wait, then reuse it
        if (venv / ".ready").exists():
            validate_cache.touch(venv)  # last use, for eviction
            return python, ""
        shutil.rmtree(venv, ignore_errors=True)
        reqs = cache_dir / f".{venv.name}.txt"
        reqs.write_text("\n".join(["pytest", *lines]) + "\n", encoding="utf-8")
        try:
            if minor == here:
                subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True,
                               text=True, timeout=120)
            else:
                # uv fetches a managed interpreter once into the cache dir (HOME is / in the image, not
                # writable); --seed puts pip in the venv so the install below is the same either way.
                proc_run([_uv(), "venv", "--seed", "--python", minor, str(venv)], check=True,
                         capture_output=True, text=True, timeout=VENV_BUILD_TIMEOUT_S,
                         env=child_env(UV_PYTHON_INSTALL_DIR=str(cache_dir / ".uv-python"),
                                       UV_CACHE_DIR=str(cache_dir / ".uv-cache"),
                                       UV_PYTHON_DOWNLOADS="automatic", UV_NO_CONFIG="1"))
            # pip's default cache is under HOME, which is / (unwritable) for the container's uid.
            proc_run([str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "-r", str(reqs)], check=True, capture_output=True, text=True,
                           timeout=VENV_BUILD_TIMEOUT_S,
                           env=child_env(PIP_CACHE_DIR=str(cache_dir / ".pip-cache")))
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(venv, ignore_errors=True)
            return None, ((exc.stderr or exc.stdout or "").strip()[-800:] or str(exc))
        except (subprocess.TimeoutExpired, OSError) as exc:
            shutil.rmtree(venv, ignore_errors=True)
            return None, str(exc)
        (venv / ".ready").write_text(key, encoding="utf-8")
        logger.info("built test venv %s for %s", venv.name, root)
        validate_cache.evict_in_background(cache_dir)
        return python, ""


def _lint_files(tree: Path, files: list[str], timeout_s: int) -> ValidationResult:
    """Fatal-only lint of the changed files: ruff when installed, else a compile check.

    A file name from the patch must never be read as an option (``--fix``, ``--config=...``) or as
    py_compile's ``-`` (stdin), so one that starts with ``-`` is passed as ``./<name>``."""
    if not files:  # ruff with no paths lints the whole tree; py_compile with none can read stdin
        return ValidationResult(passed=True, tool="lint_only", duration_ms=0, errors=[],
                                raw_output="no files to lint")
    files = [f"./{f}" if f.startswith("-") else f for f in files]
    if shutil.which("ruff"):
        return _exec(["ruff", "check", "--isolated", "--no-cache", "--select", _RUFF_SELECT, *files],
                     cwd=tree, tool="lint_only", timeout_s=timeout_s)
    import sys

    return _exec([sys.executable, "-m", "py_compile", *files], cwd=tree, tool="lint_only", timeout_s=timeout_s)


def _run_project_tests(root: Path, python: Path, timeout_s: int,
                       only: list[str] | None = None) -> tuple[str, ValidationResult]:
    """Run *root*'s tests. Returns ``(outcome, result)``: ``ran``, ``no_tests`` or ``env_gap``.

    ``env_gap``: collection failed on a missing module, so the env, not the patch, is the
    problem (a dependency the project doesn't declare, such as torch from a base image).
    *only* limits the run to those test ids (a rerun of suspected flakes)."""
    import os

    paths = [str(root / "src")] if (root / "src").is_dir() else []
    env = child_env(PYTHONPATH=os.pathsep.join([*paths, str(root)]), PYTHONDONTWRITEBYTECODE="1")
    t0 = time.monotonic()
    try:
        proc = proc_run([str(python), "-m", "pytest", "-q", "--tb=short", "-rfE", "-p", "no:cacheprovider",
                         *(["--", *only] if only else [])],
                              cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return "ran", ValidationResult(passed=False, tool="pytest", duration_ms=int((time.monotonic() - t0) * 1000),
                                       errors=[f"tests timed out after {timeout_s}s"], raw_output="")
    except OSError as exc:  # the env's interpreter is gone or broken: an env problem, not the patch's
        return "env_gap", ValidationResult(passed=False, tool="pytest", duration_ms=0, errors=[str(exc)],
                                           raw_output=f"ImportError: cannot run tests: {exc}")
    full = proc.stdout + proc.stderr
    out = full[-_OUTPUT_LIMIT:]
    # pytest reports failures (tracebacks, the -rfE summary) on stdout; stderr is often just noise
    error = proc.stdout.strip()[-1000:] or proc.stderr.strip()[-1000:]
    res = ValidationResult(passed=proc.returncode == 0, tool="pytest",
                           duration_ms=int((time.monotonic() - t0) * 1000),
                           errors=[] if proc.returncode == 0 else [error],
                           raw_output=out)
    if proc.returncode == 5:  # pytest: no tests collected
        return "no_tests", res
    # only collection errors, at least one from a missing module, and no test ran at all. Judged on
    # the whole output: the first ModuleNotFoundError can fall outside the stored tail.
    if proc.returncode != 0 and _ENV_GAP_RE.search(full) and not _RESULT_COUNTS_RE.search(full):
        return "env_gap", res
    return "ran", res


_VERSION_STUB = (
    "# Stub written by ai-code-auditor validate_patch: the real file is generated at build time.\n"
    "__version__ = version = \"0.0.0+validate\"\n"
    "__version_tuple__ = version_tuple = (0, 0, 0, \"validate\")\n"
)


def stub_version_files(root: Path) -> list[str]:
    """Write a stub for each build-generated version module *root*'s pyproject declares but lacks.

    hatch-vcs (``[tool.hatch.build.hooks.vcs] version-file``) and setuptools-scm (``version_file`` /
    ``write_to``) write that module at build time and it is gitignored, so a clean tree lacks it
    and any ``from pkg._version import ...`` fails, pushing validation to lint-only. Paths outside
    *root* are ignored. Returns the paths written, relative to *root*."""
    import tomllib

    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tool = _table(data.get("tool"))
    hooks = _table(_table(_table(tool.get("hatch")).get("build")).get("hooks"))
    scm = _table(tool.get("setuptools_scm"))
    wanted = [_table(hooks.get("vcs")).get("version-file"), scm.get("version_file"), scm.get("write_to")]
    written: list[str] = []
    resolved_root = root.resolve()
    for rel in dict.fromkeys(w for w in wanted if isinstance(w, str) and w.endswith(".py")):
        target = (root / rel).resolve()
        if not target.is_relative_to(resolved_root) or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_VERSION_STUB, encoding="utf-8")
        except OSError:
            continue
        written.append(rel)
    return written


def _failed_ids(output: str) -> set[str]:
    return set(_FAILED_ID_RE.findall(output))


_SUMMARY_COUNT_RE = re.compile(r"\b(\d+) (failed|errors?)\b")


def _reported_failures(output: str) -> int:
    """Failed plus errored tests on pytest's final summary line (``3 failed, 1 error in 2s``), 0 if none."""
    last = next((ln for ln in reversed(output.splitlines()) if re.search(r"\bin [\d.]+s\b", ln)), "")
    return sum(int(n) for n, _ in _SUMMARY_COUNT_RE.findall(last))


def _has_project(root: Path) -> bool:
    return any((root / m).exists() for m in _PROJECT_MARKERS) or (root / "tests").is_dir()


def validate_patch_tree(
    repo: RepoConfig, tree: Path, changed: list[str], *, timeout_s: int = 120, cache_dir: Path | None = None,
    base_tree: "Callable[[], Path | None] | None" = None, node_cache_dir: Path | None = None,
) -> ValidationResult:
    """Validate a patched clean *tree*: Python projects and npm projects the patch touches.

    Python: lint the changed .py files, then test each touched project. Passes when lint passes
    and every project whose tests ran passed; when no tests could run (none exist, the env failed
    to build, or a module was missing), the lint result stands (``lint_only``) with a note saying why.

    TypeScript/JavaScript (:mod:`auditor.validate_node`): each touched npm project runs its
    typecheck, lint, ``*:check`` and test scripts. A changed TS/JS file that got no check at all
    (npm missing, install failed, no package.json) makes the result *skipped*, never passed, so a
    pack isn't resolved on the strength of checks that never looked at it.

    ``tool`` names what ran, joined with ``+`` for a mixed patch (``pytest+npm_checks``).
    Patches with neither go to :func:`validate_repo` for the repo's own language.

    *base_tree* returns the same tree without the patch (built on demand). When a test or check
    fails, it runs there too, and only failures the patch introduced count."""
    from auditor import validate_node

    py = sorted({f for f in changed if f.endswith(".py") and (tree / f).is_file()})
    # A .py file the patch deletes or renames away still picks its project's tests: a removed
    # module breaks whatever imports it. Only files that exist are linted.
    py_projects = sorted({f for f in changed if f.endswith(".py")})
    js = validate_node.node_files(tree, changed)
    if not py_projects and not js:  # a patch that only deletes .py files still tests their projects
        return validate_repo(repo, path_override=tree, timeout_s=timeout_s)
    cache = cache_dir or Path(".validate_venvs")
    notes: list[str] = []
    ran: list[tuple[str, ValidationResult]] = []
    tools: list[str] = []
    duration = 0
    if py_projects:
        if py:
            lint = _lint_files(tree, py, timeout_s)
            if not lint.passed:
                return lint
            duration += lint.duration_ms
        py_ran = _test_python_projects(tree, py_projects, cache, base_tree, timeout_s, notes)
        ran += py_ran
        tools.append("pytest" if py_ran else "lint_only")
    unvalidated: list[str] = []
    if js:
        node = validate_node.validate_node_projects(
            tree, js, timeout_s=timeout_s, cache_dir=node_cache_dir or cache.parent / "_validate_node",
            base_tree=base_tree)
        ran += node.ran
        notes += node.notes
        unvalidated = node.unvalidated
        if node.ran:
            tools.append(validate_node.SYNTAX_TOOL if node.syntax_only else validate_node.TOOL)
    tool = "+".join(dict.fromkeys(tools)) or "skipped"
    duration += sum(r.duration_ms for _, r in ran)
    note = "\n".join(notes)
    failed = [(lbl, r) for lbl, r in ran if not r.passed]
    if failed:
        return ValidationResult(
            passed=False, tool=tool, duration_ms=duration,
            errors=[e for _, r in failed for e in r.errors],
            raw_output="\n".join(f"== {lbl}\n{r.raw_output}" for lbl, r in failed)[-_OUTPUT_LIMIT:],
        )
    parts = []
    if py_labels := [lbl for lbl, r in ran if r.tool == "pytest"]:
        parts.append("tests passed: " + ", ".join(py_labels))
    if js_ran := [f"{lbl} ({r.raw_output})" if r.tool == validate_node.TOOL else lbl
                  for lbl, r in ran if r.tool != "pytest"]:
        parts.append("npm checks passed: " + ", ".join(js_ran))
    head = "; ".join(parts) or "lint passed; no tests ran"
    if notes and ran:
        head += " (see notes)"
    if unvalidated:
        reason = "TypeScript/JavaScript not validated: " + "; ".join(unvalidated)
        return ValidationResult(passed=False, tool="skipped", duration_ms=duration, errors=[],
                                raw_output="\n".join(x for x in (reason, head, note) if x)[-_OUTPUT_LIMIT:],
                                skipped_reason=reason[:1000])
    return ValidationResult(
        passed=True, tool=tool, duration_ms=duration, errors=[],
        raw_output="\n".join(x for x in (head, note) if x)[-_OUTPUT_LIMIT:],
    )


def _test_python_projects(
    tree: Path, py: list[str], cache: Path, base_tree: "Callable[[], Path | None] | None", timeout_s: int,
    notes: list[str],
) -> list[tuple[str, ValidationResult]]:
    """Run the tests of each Python project *py* touches; returns (label, result) for those that ran."""
    ran: list[tuple[str, ValidationResult]] = []
    for root in sorted({project_root(tree, f) for f in py}):
        label = str(root.relative_to(tree.resolve())) if root != tree.resolve() else "."
        if not _has_project(root):
            mine = [str((tree / f).resolve().relative_to(root)) for f in py if project_root(tree, f) == root]
            groups = loose_tests(root, mine)
            if not groups:
                notes.append(f"{label}: no project (pyproject, setup.py, requirements or tests/) and no "
                             f"test_*.py beside {', '.join(mine)}; lint only")
                continue
            ran += _test_loose(root, tree, groups, cache, base_tree, timeout_s, notes)
            continue
        python, err = ensure_test_venv(root, cache)
        if python is None:
            notes.append(f"{label}: test env did not build, lint only: {err[:300]}")
            continue
        stubbed = stub_version_files(root)
        if stubbed:
            notes.append(f"{label}: wrote a stub for the build-generated {', '.join(stubbed)}")
        outcome, res = _run_project_tests(root, python, timeout_s)
        if outcome == "no_tests":
            notes.append(f"{label}: no tests collected, lint only")
        elif outcome == "env_gap":
            notes.append(f"{label}: tests need a module the env lacks, lint only: {_env_gap_line(res)}")
        elif not res.passed and base_tree is not None:
            res = _against_base(root, tree, base_tree, python, timeout_s, res, label, notes)
            ran.append((label, res))
        else:
            ran.append((label, res))
    return ran


def _test_loose(
    root: Path, tree: Path, groups: dict[str, list[str]], cache: Path,
    base_tree: Callable[[], Path | None] | None, timeout_s: int, notes: list[str],
) -> list[tuple[str, ValidationResult]]:
    """Run a markerless root's loose tests (#102), one pytest run per directory so one file that
    fails to collect doesn't stop the rest. They run from *root*, as the repo's CI runs them."""
    ran: list[tuple[str, ValidationResult]] = []
    python, err = ensure_test_venv(root, cache, loose_test_deps(root, [t for ts in groups.values() for t in ts]))
    if python is None:
        notes.append(f"{_label(root, tree)}: test env did not build, lint only: {err[:300]}")
        return ran
    for d, tests in groups.items():
        label = d if root == tree.resolve() else f"{_label(root, tree)}/{d}"
        outcome, res = _run_project_tests(root, python, timeout_s, only=tests)
        if outcome == "no_tests":
            notes.append(f"{label}: no tests collected from {', '.join(tests)}, lint only")
        elif outcome == "env_gap":
            notes.append(f"{label}: tests need a module the env lacks, lint only: {_env_gap_line(res)}")
        else:
            if not res.passed and base_tree is not None:
                res = _against_base(root, tree, base_tree, python, timeout_s, res, label, notes, only=tests)
            ran.append((label, res))
    return ran


def _label(root: Path, tree: Path) -> str:
    return str(root.relative_to(tree.resolve())) if root != tree.resolve() else "."


def _env_gap_line(res: ValidationResult) -> str:
    m = _ENV_GAP_RE.search(res.raw_output)
    return next((ln for ln in res.raw_output.splitlines() if m and m.group(1) in ln), "").strip()[:200]


def _against_base(
    root: Path, tree: Path, base_tree: "Callable[[], Path | None]", python: Path, timeout_s: int,
    res: ValidationResult, label: str, notes: list[str], only: list[str] | None = None,
) -> ValidationResult:
    """Re-judge a failing run by what the patch broke: failures also present without it don't count.

    A test that fails only with the patch is run once more on the patched tree, and one that then
    passes is a flake, not the patch's (#97). Tests that *error* on both trees are reported apart
    from ones that fail on both: an error at setup usually means the test env, not the repo."""
    patched = _failed_ids(res.raw_output)
    if not patched or _reported_failures(res.raw_output) > len(patched):
        # the stored output is a tail: when pytest counts more failures than it shows ids for, the
        # cut-off ones can't be compared, so the failure stands
        return res
    try:
        rel = root.resolve().relative_to(tree.resolve())
    except ValueError:
        return res
    base = base_tree()
    if base is None:
        return res
    base_root = base / rel
    if not base_root.is_dir():
        return res
    stub_version_files(base_root)
    base_only = [t for t in only if (base_root / t).is_file()] if only else None
    if only and not base_only:  # every test file is new in the patch: nothing to compare with
        return res
    outcome, base_res = _run_project_tests(base_root, python, timeout_s, only=base_only)
    if outcome != "ran":
        return res
    duration = res.duration_ms + base_res.duration_ms
    already = _failed_ids(base_res.raw_output)
    new = sorted(patched - already)
    flaky: list[str] = []
    if new:
        r_outcome, rerun = _run_project_tests(root, python, timeout_s, only=new)
        duration += rerun.duration_ms
        if r_outcome == "ran" and rerun.passed:
            flaky, new = new, []
        elif r_outcome == "ran" and (still := _failed_ids(rerun.raw_output)):
            flaky, new = sorted(set(new) - still), sorted(set(new) & still)
    if new:
        res.raw_output = (f"new failures from this patch: {', '.join(new)}\n" + res.raw_output)[-_OUTPUT_LIMIT:]
        return res
    if flaky:
        notes.append(f"{label}: {len(flaky)} test(s) failed only with the patch but passed on a rerun (flaky), "
                     f"not counted: {', '.join(flaky)[:400]}")
    both = patched & already
    errored = both & set(_ERRORED_ID_RE.findall(res.raw_output)) & set(_ERRORED_ID_RE.findall(base_res.raw_output))
    if errored:
        notes.append(f"{label}: {len(errored)} test(s) error on the base branch too (likely the test env, "
                     f"not the repo), not counted: {', '.join(sorted(errored))[:400]}")
    if failed := both - errored:
        notes.append(f"{label}: {len(failed)} test(s) fail on the base branch too, not counted: "
                     f"{', '.join(sorted(failed))[:400]}")
    return ValidationResult(passed=True, tool="pytest", duration_ms=duration, errors=[], raw_output=res.raw_output)
