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
    cmd = ["npm", "test", "--", "--passWithNoTests"]
    return _exec(cmd, cwd=repo_path, tool="npm_test", timeout_s=timeout_s)


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
