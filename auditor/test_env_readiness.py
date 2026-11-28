"""Test-env readiness: can ``validate_patch`` run each project's tests, or will it fall back to lint?

validate_patch builds a test env per project from what the project declares (``validator.ensure_test_venv``
for Python, ``validate_node.ensure_node_modules`` for npm). When a project's tests import something
it doesn't declare, pytest stops at collection, *every* patch to that project validates on lint
alone, and the evidence bar (#58) can only leave its packs unverified or waived. That is the
largest source of lint-only resolutions, and nothing else surfaces it before an agent hits it.

This check exports each repo's ``dev`` tip (``validate_base``, the mirror is never written), finds
the projects the validator would map files to, builds their envs with the validator's own code and
cache, and runs collection only (``pytest --collect-only``; no test runs). One project, one status:

==============  =============================================================================
ready           collection succeeded; ``tests`` is the count
import_errors   collection failed on missing modules (``missing``): pytest collects nothing, so
                every patch to the project validates lint-only until they are declared
collect_errors  collection failed for another reason (syntax error, conftest error, ...)
no_tests        nothing to collect
env_failed      the venv or ``npm ci`` failed (``error``)
no_checks       npm project with no typecheck, lint, ``*:check`` or test script
==============  =============================================================================

It runs in the nightly repo-audit tail and with ``ai-audit test-env``, never in the API process:
building envs takes minutes. ``reports/test_env_readiness.json`` is what the API serves.

It has to run on the interpreter validate_patch uses (#136): which pins install, and each env's cache
key, depend on the Python version. That's the API image's (``ARG PYTHON_VERSION`` in the Dockerfile,
``AI_AUDIT_VALIDATOR_PYTHON`` in the image or to override). Run anywhere else, such as the host's
nightly, the check runs itself in the API image (``docker compose run audit-api ai-audit test-env``),
and refuses when it can't, rather than report another interpreter's statuses. A report written on
another interpreter is served with ``stale_interpreter`` set.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auditor.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

REPORT_NAME = "test_env_readiness.json"
COLLECT_TIMEOUT_S = 300
# Directories whose files the validator would never be asked about, or that only hold fixtures.
_SKIP_PARTS = frozenset({"node_modules", ".git", ".venv", "venv", "__pycache__", "fixtures", "testdata",
                         "site-packages", ".tox"})
_MISSING_RE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")
_CANT_IMPORT_RE = re.compile(r"ImportError: cannot import name '([^']+)' from '([^']+)'")
_COLLECTED_RE = re.compile(r"(\d+) tests? collected")
_ERRORS_RE = re.compile(r"(\d+) errors?\b")
_ERROR_FILE_RE = re.compile(r"^ERROR (\S+)", re.M)
BAD = ("import_errors", "collect_errors", "env_failed")


def _skipped(rel: Path) -> bool:
    return any(p in _SKIP_PARTS for p in rel.parts)


def python_projects(tree: Path) -> list[Path]:
    """Project roots the validator maps this tree's .py files to (``validator.project_root``)."""
    from auditor.validator import _has_project, project_root

    roots: set[Path] = set()
    tree = tree.resolve()
    for f in tree.rglob("*.py"):
        rel = f.relative_to(tree)
        if _skipped(rel):
            continue
        root = project_root(tree, str(rel))
        if _has_project(root):
            roots.add(root)
    return sorted(roots)


def node_projects(tree: Path) -> list[Path]:
    tree = tree.resolve()
    return sorted(p.parent for p in tree.rglob("package.json") if not _skipped(p.relative_to(tree)))


def parse_collect(output: str, returncode: int) -> dict[str, Any]:
    """Status and details from ``pytest --collect-only -q`` output."""
    m = _COLLECTED_RE.search(output)
    tests = int(m.group(1)) if m else len([ln for ln in output.splitlines() if "::" in ln])
    if returncode == 5:
        return {"status": "no_tests", "tests": 0}
    if returncode == 0:
        return {"status": "ready", "tests": tests}
    missing = sorted({x.split(".")[0] for x in _MISSING_RE.findall(output)}
                     | {f"{mod} ({name})" for name, mod in _CANT_IMPORT_RE.findall(output)})
    files = sorted(set(_ERROR_FILE_RE.findall(output)))
    out: dict[str, Any] = {"tests": tests, "error_files": files[:30]}
    if missing:
        return {"status": "import_errors", "missing": missing, **out}
    tail = "\n".join(output.strip().splitlines()[-15:])
    return {"status": "collect_errors", "error": tail[-1200:], **out}


def check_python(root: Path, cache: Path, timeout_s: int = COLLECT_TIMEOUT_S) -> dict[str, Any]:
    """Build *root*'s test venv as validate_patch does, then collect its tests."""
    from auditor.validator import ensure_test_venv, requires_python, venv_python_version

    python, err = ensure_test_venv(root, cache)
    if python is None:
        return {"status": "env_failed", "error": err[-1200:], "requires_python": requires_python(root)}
    res = _collect(root, python, timeout_s)
    res["python"] = venv_python_version(python)  # per project: requires-python may pick another (#141)
    return res


def _collect(root: Path, python: Path, timeout_s: int) -> dict[str, Any]:
    from auditor.validator import stub_version_files

    stub_version_files(root)
    # Same interpreter, PYTHONPATH and cwd as validator._run_project_tests.
    paths = [str(root / "src")] if (root / "src").is_dir() else []
    from auditor.proc import child_env

    env = child_env(PYTHONPATH=os.pathsep.join([*paths, str(root)]), PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run([str(python), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                              cwd=root, env=env, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "collect_errors", "error": f"collection timed out after {timeout_s}s"}
    except OSError as exc:
        return {"status": "env_failed", "error": str(exc)}
    return parse_collect(proc.stdout + proc.stderr, proc.returncode)


def check_node(root: Path, cache: Path) -> dict[str, Any]:
    """Install *root*'s locked deps as validate_patch does; report which checks it declares."""
    from auditor import validate_node

    checks = [s for s, _ in validate_node.project_checks(root)]
    if not checks:
        return {"status": "no_checks"}
    modules, err = validate_node.ensure_node_modules(root, cache)
    if modules is None:
        return {"status": "env_failed", "error": err[-1200:], "checks": checks}
    return {"status": "ready", "checks": checks}


def check_tree(tree: Path, *, venv_cache: Path, node_cache: Path,
               timeout_s: int = COLLECT_TIMEOUT_S) -> list[dict[str, Any]]:
    tree = tree.resolve()
    out = []
    for kind, roots, fn in (("python", python_projects(tree), lambda r: check_python(r, venv_cache, timeout_s)),
                            ("node", node_projects(tree), lambda r: check_node(r, node_cache))):
        for root in roots:
            t0 = time.monotonic()
            try:
                res = fn(root)
            except Exception as exc:  # noqa: BLE001 — one project never sinks the repo's report
                res = {"status": "env_failed", "error": f"check crashed: {exc}"[:600]}
            rel = str(root.relative_to(tree)) if root != tree else "."
            out.append({"project": rel, "kind": kind, **res, "ms": int((time.monotonic() - t0) * 1000)})
    return out


def check_repo(name: str, repo_path: Path, *, output_dir: Path, venv_cache: Path, node_cache: Path,
               timeout_s: int = COLLECT_TIMEOUT_S) -> dict[str, Any]:
    """Export *repo_path*'s ``dev`` tip (validate_patch's base) and check every project in it."""
    from auditor import validate_base

    base = validate_base.resolve_base(repo_path, output_dir=output_dir)
    with tempfile.TemporaryDirectory(prefix=f"test-env-{name}-") as tmp:
        tree = Path(tmp) / "tree"
        validate_base.export_tree(base, repo_path, tree)
        projects = check_tree(tree, venv_cache=venv_cache, node_cache=node_cache, timeout_s=timeout_s)
    return {"base_sha": base.sha, "base_source": base.source, "fetch_error": base.fetch_error or "",
            "projects": projects}


def summarize(repos: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    failing: dict[str, list[str]] = {}
    for name, r in repos.items():
        for p in r.get("projects") or []:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
            if p["status"] in BAD:
                failing.setdefault(name, []).append(p["project"])
        if r.get("error"):
            counts["repo_error"] = counts.get("repo_error", 0) + 1
            failing.setdefault(name, []).append("(repo)")
    return {"by_status": counts, "failing": failing}


def _node_version() -> str:
    from auditor import validate_node

    return validate_node._node_version()


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_TIMEOUT_S = 4 * 3600


class InterpreterMismatch(RuntimeError):
    """This interpreter isn't the validator's, and the check couldn't run in the API image."""


def validator_python() -> str:
    """``major.minor`` of the interpreter validate_patch runs under, or "" when unknown."""
    v = os.getenv("AI_AUDIT_VALIDATOR_PYTHON", "").strip()
    if v:
        return ".".join(v.split(".")[:2])
    try:
        text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^ARG\s+PYTHON_VERSION=(\d+\.\d+)", text, re.M) or re.search(r"^FROM\s+python:(\d+\.\d+)", text, re.M)
    return m.group(1) if m else ""


def this_python() -> str:
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def stale_interpreter(report: dict[str, Any]) -> str:
    """Why *report* doesn't describe the validator's interpreter, or "" when it does (or can't tell)."""
    want, had = validator_python(), ".".join(str(report.get("python") or "").split(".")[:2])
    return f"built on Python {had}, but validate_patch runs on {want}" if want and had and had != want else ""


def _in_api_image(out: Path, repos: list[str] | None, workers: int,
                  timeout_s: int = COLLECT_TIMEOUT_S) -> dict[str, Any]:
    """Run the check in the API's image, where validate_patch runs, and return the report it wrote."""
    mounted = REPO_ROOT / "reports"  # docker-compose.yml mounts ./reports at the image's output dir
    if out.resolve() != mounted.resolve():
        raise InterpreterMismatch(f"the API image writes its report to {mounted}, not {out}: run it there by hand")
    # A known name, so a timeout can remove the container: killing the compose client leaves it running.
    name = f"ai-audit-test-env-{os.getpid()}-{int(time.time())}"
    cmd = ["docker", "compose", "run", "--rm", "--no-deps", "-T", "--name", name, "audit-api", "ai-audit", "test-env",
           "--workers", str(workers)]
    for r in repos or []:
        cmd += ["--repo", r]
    if timeout_s != COLLECT_TIMEOUT_S:  # only when set, so an image without the flag still runs the default
        cmd += ["--timeout", str(timeout_s)]
    logger.info("test env: Python %s here, %s in the API image: running the check there", this_python(),
                validator_python())
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=CONTAINER_TIMEOUT_S,
                              check=False)
    except subprocess.TimeoutExpired as exc:
        try:
            subprocess.run(["docker", "rm", "-f", name], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
                           check=False)
        except (OSError, subprocess.TimeoutExpired) as rm_exc:
            logger.warning("test env: could not remove container %s: %s", name, rm_exc)
        raise InterpreterMismatch(f"the check in the API image timed out after {CONTAINER_TIMEOUT_S}s") from exc
    report = load_report(out)
    if proc.returncode != 0 or report is None or stale_interpreter(report):
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise InterpreterMismatch(f"the check in the API image failed (exit {proc.returncode}): {' | '.join(tail)}"[:600])
    return report


def _output_dir(output_dir: str | Path | None, cfg: Any = None) -> Path:
    """Where the report goes: the argument, ``AI_AUDIT_OUTPUT_DIR``, the ecosystem config's, or ``reports``."""
    if output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR"):
        return Path(output_dir or os.environ["AI_AUDIT_OUTPUT_DIR"])
    if cfg is None:
        from auditor.config import load_ecosystem_config

        cfg = load_ecosystem_config()
    return Path(cfg.output_dir or "reports")


def run(output_dir: str | Path | None = None, *, repos: list[str] | None = None, workers: int = 3,
        timeout_s: int = COLLECT_TIMEOUT_S, write: bool = True) -> dict[str, Any]:
    """Check every enabled repo in the ecosystem config (or *repos*); write the report. On an
    interpreter other than the validator's, run in the API image instead, or refuse (#136)."""
    want = validator_python()
    if want and want != this_python():
        out = _output_dir(output_dir)
        if write and shutil.which("docker") and os.getenv("AI_AUDIT_TEST_ENV_IN_IMAGE", "1").strip().lower() not in (
                "0", "false", "no", "off"):
            return _in_api_image(out, repos, workers, timeout_s)
        raise InterpreterMismatch(f"Python {this_python()} here, but validate_patch runs on {want}: the report would "
                                  "describe the wrong interpreter. Run it in the API image "
                                  "(docker compose run --rm --no-deps audit-api ai-audit test-env)")
    from auditor.config import load_ecosystem_config
    from auditor.repo_resolver import resolve_repo_path

    cfg = load_ecosystem_config()
    out = _output_dir(output_dir, cfg)
    venv_cache = Path(os.getenv("AI_AUDIT_VALIDATE_VENV_DIR") or out / "_validate_venvs")
    node_cache = Path(os.getenv("AI_AUDIT_VALIDATE_NODE_DIR") or out / "_validate_node")
    wanted = [r for r in cfg.repos if r.enabled and (not repos or r.name in repos)]

    def one(r) -> tuple[str, dict[str, Any]]:
        try:
            return r.name, check_repo(r.name, resolve_repo_path(r), output_dir=out, venv_cache=venv_cache,
                                      node_cache=node_cache, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("test env check failed for %s: %s", r.name, exc)
            return r.name, {"error": str(exc)[:600], "projects": []}

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = dict(sorted(pool.map(one, wanted)))
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0],
        "node": _node_version(),
        "duration_s": round(time.monotonic() - t0, 1),
        "repos": results,
        "summary": summarize(results),
    }
    if write:
        if repos:  # a partial run updates those repos in the last full report
            prev = load_report(out) or {}
            if prev.get("stale_interpreter"):  # another interpreter's statuses: don't carry them over
                prev = {}
            merged = {**(prev.get("repos") or {}), **results}
            report["repos"] = dict(sorted(merged.items()))
            report["summary"] = summarize(report["repos"])
        out.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out / REPORT_NAME, json.dumps(report, indent=1, default=str))
    return report


def load_report(output_dir: str | Path) -> dict[str, Any] | None:
    """The last report, with ``stale_interpreter`` set when it describes another interpreter."""
    try:
        report = json.loads((Path(output_dir) / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):  # a list or scalar is no usable report
        return None
    report["stale_interpreter"] = stale_interpreter(report)
    return report


def enabled() -> bool:
    return os.getenv("AI_AUDIT_TEST_ENV_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")


def run_quietly(output_dir: str | Path | None = None, **kw: Any) -> dict[str, Any]:
    """Pipeline tail: never fail the caller."""
    if not enabled():
        return {"skipped": "AI_AUDIT_TEST_ENV_CHECK=0"}
    try:
        return run(output_dir, **kw)["summary"]
    except InterpreterMismatch as exc:
        logger.warning("test env check skipped: %s", exc)
        return {"skipped": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("test env check failed: %s", exc)
        return {"error": str(exc)[:300]}
