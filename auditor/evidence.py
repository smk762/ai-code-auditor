"""What a passing validate_patch has to show before a pack counts as resolved (#58).

A lint pass proves the code parses, not that the issue is gone. With ``pack_id`` set,
validate_patch only resolves the pack when the evidence fits it, and otherwise marks it
``unverified``. That status means passed but unproven: still open work, at lower priority.

=================  ==========================================================================
Evidence kind      Resolves when
=================  ==========================================================================
``repro``          Bug and security packs (and finding packs of unknown type) that change code:
                   the patch adds or changes a test that **fails on the dev tree and passes on
                   the patched tree**. The patch's test files are copied onto a clean dev tree
                   and run there (pytest; vitest for TS/JS projects whose test script uses it).
                   A test that can't even be collected on dev (it imports something the patch
                   adds) doesn't count, because it shows a new API, not a fixed bug.
                   The files are copied whole, so a helper defined inside a test file runs patched
                   on dev too and can't show the bug. Taking dev's helpers into the patched test file
                   would let a test-side change manufacture the dev failure, so that case needs a
                   waiver (a test-only patch resolves as ``test_only`` without this run).
``tests``          Other packs that change code: the project's tests (or npm checks) ran and
                   passed. Lint alone is not enough, and neither is a run where every
                   test was skipped (the same goes for ``test_only`` and ``structure``).
``test_only``      The patch changes only tests (the fix *is* the test): those tests ran.
``non_code``       The patch changes only docs or config (no code, no tests).
``structure``      Structural packs (``module_cycle``, ``private_import``, #154): the scan finds the
                   violation on the dev tree and not on the patched one (``structure.judge``), **and**
                   the tests ran. A waiver stands in for the tests, never for the structure.
``waiver``         The caller gave ``evidence_waiver`` with a reason (≥ 20 characters), e.g.
                   the behaviour needs hardware or a live service. Recorded on the pack and
                   counted in the trust metrics, so a habit of waiving is visible.
=================  ==========================================================================

``AI_AUDIT_EVIDENCE_BAR=0`` restores the old rule (any pass resolves), for emergencies.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from auditor.proc import child_env, run as proc_run

logger = logging.getLogger(__name__)

REPRO_KINDS = frozenset({"bug", "security", ""})  # "" = a finding pack from before finding_type was stored
STRUCTURE_KINDS = frozenset({"module_cycle", "private_import"})  # judged by structure.judge (#154)
FINDING_PACK_EXCLUDE = frozenset({"world_model", "architecture", "extraction", "invariants"}) | STRUCTURE_KINDS
MIN_WAIVER_CHARS = 20
_OUTPUT_LIMIT = 6000

_PY_TEST_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
_JS_TEST_RE = re.compile(r"(^|/)(__tests__/|[^/]*\.(test|spec)\.(ts|tsx|js|jsx|mts|mjs|cts|cjs)$)")
_NON_CODE_RE = re.compile(
    r"(\.(md|rst|txt|adoc|ya?ml|json|toml|ini|cfg|conf|env\.example|example|lock|svg|png|jpe?g|gif|ico)$"
    r"|(^|/)(LICENSE|NOTICE|CODEOWNERS|Dockerfile|\.gitignore|\.dockerignore|\.editorconfig)[^/]*$)",
    re.IGNORECASE,
)
_PYTEST_OUTCOME_RE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+?)(?:\s+-\s+.*)?$", re.MULTILINE)
# A failing subtest (pytest >= 9 built in, or pytest-subtests) gets its own summary line, with the
# subtest's message and params before the test id: ``SUBFAILED[msg] (i=1) tests/t.py::T::test_a - ...``.
# pytest 9 still lists a unittest-style test whose subtests failed as ``PASSED``, so this line is the
# only sign it failed.
_PYTEST_SUBFAIL_RE = re.compile(r"^SUBFAIL(?:ED)?\b.*?\s(\S+?\.py::\S+)", re.MULTILINE)


def enabled() -> bool:
    return os.getenv("AI_AUDIT_EVIDENCE_BAR", "1").strip().lower() not in {"0", "false", "no", "off"}


# ── classifying a patch ──────────────────────────────────────────────────────

_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__)/")


def is_test_file(path: str) -> bool:
    """A test, or anything a test may load (fixtures, data) under a tests directory. All of it is
    copied onto the dev tree, so a test never fails there just for lack of its own fixture."""
    if _TEST_DIR_RE.search(path):
        return True
    return bool(_PY_TEST_RE.search(path) if path.endswith(".py") else _JS_TEST_RE.search(path))


def classify_changes(changed: list[str]) -> dict[str, list[str]]:
    """Split a patch's paths into ``tests``, ``non_code`` and ``code``."""
    out: dict[str, list[str]] = {"tests": [], "non_code": [], "code": []}
    for p in changed:
        if is_test_file(p):
            out["tests"].append(p)
        elif _NON_CODE_RE.search(p):
            out["non_code"].append(p)
        else:
            out["code"].append(p)
    return out


def pack_finding_type(pack: dict[str, Any]) -> str:
    return str((pack.get("narrative") or {}).get("finding_type") or "").strip().lower()


def needs_repro(pack: dict[str, Any]) -> bool:
    return pack.get("kind") not in FINDING_PACK_EXCLUDE and pack_finding_type(pack) in REPRO_KINDS


# ── running the patch's tests on both trees ──────────────────────────────────

@dataclass
class ReproResult:
    """Per-test outcomes of the patch's test files on the patched and the dev tree."""

    tool: str = ""
    patched: dict[str, str] = field(default_factory=dict)  # test id -> passed | failed | skipped | error
    base: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    output: str = ""
    # repro tests of a shared multi-pack run that belong to other packs, not this one (for_pack)
    scoped_out: list[str] = field(default_factory=list)

    @property
    def repro(self) -> list[str]:
        return sorted(t for t, s in self.patched.items() if s == "passed" and self.base.get(t) == "failed")

    @property
    def ran(self) -> bool:
        return bool(self.patched)

    def merge(self, other: ReproResult) -> None:
        self.tool = "+".join(dict.fromkeys(t for t in (self.tool, other.tool) if t))
        self.patched.update(other.patched)
        self.base.update(other.base)
        self.notes += other.notes
        self.output = (self.output + "\n" + other.output).strip()[-_OUTPUT_LIMIT:]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["repro"] = self.repro
        d["output_sha256"] = hashlib.sha256(self.output.encode()).hexdigest()
        d.pop("output")
        return d


def _copy_into(src_tree: Path, dst_tree: Path, files: list[str]) -> None:
    """Copy *files* from one tree to the other. Paths come from an untrusted patch, so an absolute or
    ``..`` path, or a symlink resolving outside either tree, is skipped rather than followed."""
    src_root, dst_root = src_tree.resolve(), dst_tree.resolve()
    for f in files:
        if Path(f).is_absolute() or ".." in Path(f).parts:
            continue
        src, dst = src_tree / f, dst_tree / f
        if not src.resolve().is_relative_to(src_root) or not dst.resolve().is_relative_to(dst_root):
            continue
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def _inside(path: Path, parent: Path) -> str | None:
    """*path* relative to *parent*, both resolved, or None when it isn't inside (a symlinked tree
    or an escaping path must become a note, not a ValueError that aborts the whole repro)."""
    try:
        return str(path.resolve().relative_to(parent.resolve()))
    except ValueError:
        return None


_PYTEST_RANK = {"skipped": 0, "passed": 1, "failed": 2, "error": 3}


def parse_pytest_outcomes(output: str) -> dict[str, str]:
    """Test id -> outcome from ``pytest -rA`` short-summary lines. A collection error names a
    file, not a test, so it carries no test id and is reported separately.

    A test can be listed twice (a passing call whose teardown errors shows as ``ERROR`` and
    ``PASSED``): the worst outcome wins, whatever order the sections come in. A test with any
    failing subtest is failed, even when pytest lists the test itself as passed."""
    out: dict[str, str] = {}
    found = _PYTEST_OUTCOME_RE.findall(output) + [("FAILED", t) for t in _PYTEST_SUBFAIL_RE.findall(output)]
    for status, tid in found:
        if "::" not in tid:
            continue
        new = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error", "XPASS": "passed"}.get(status, "skipped")
        if _PYTEST_RANK[new] > _PYTEST_RANK.get(out.get(tid, ""), -1):
            out[tid] = new
    return out


def _pytest(root: Path, python: Path, files: list[str], timeout_s: int) -> tuple[dict[str, str], str]:
    paths = [str(root / "src")] if (root / "src").is_dir() else []
    env = child_env(PYTHONPATH=os.pathsep.join([*paths, str(root)]), PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = proc_run([str(python), "-m", "pytest", "-q", "-rA", "--tb=line", "-p", "no:cacheprovider",
                         "--", *files],  # a file name from the patch is never read as an option
                              cwd=root, env=env, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {}, f"timed out after {timeout_s}s"
    except OSError as exc:
        return {}, str(exc)
    out = proc.stdout + proc.stderr
    return parse_pytest_outcomes(out), out[-_OUTPUT_LIMIT:]


def python_repro(tree: Path, fresh_base: Callable[[], Path | None], tests: list[str], *, cache_dir: Path,
                 timeout_s: int = 120) -> ReproResult:
    """Run the patch's Python test files on the patched tree, then on dev with those files copied in."""
    from auditor.validator import (
        _has_project,
        ensure_test_venv,
        loose_test_deps,
        project_root,
        stub_version_files,
    )

    res = ReproResult(tool="pytest")
    py_tests = [t for t in tests if t.endswith(".py") and (tree / t).is_file()]
    if not py_tests:
        return ReproResult()
    material = [t for t in tests if (tree / t).is_file()]
    tree_r = tree.resolve()
    by_root: dict[Path, list[str]] = {}
    for t in py_tests:
        root = project_root(tree, t).resolve()
        if _inside(root, tree_r) is None:
            res.notes.append(f"{t}: its project root is outside the tree, not run")
            continue
        by_root.setdefault(root, []).append(t)
    groups: list[tuple[Path, list[str], bool]] = []
    for root, files in sorted(by_root.items()):
        if _has_project(root):
            groups.append((root, files, False))
        else:  # loose tests (#102): one run per directory, so one collection error doesn't sink the rest
            per_dir: dict[str, list[str]] = {}
            for f in files:
                per_dir.setdefault(str(Path(f).parent), []).append(f)
            groups += [(root, fs, True) for _, fs in sorted(per_dir.items())]
    base = None
    for root, files, loose in groups:
        label = root_label = _inside(root, tree_r) if root != tree_r else "."
        runnable = []
        for f in files:
            if f.endswith("conftest.py") or not re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", f):
                continue
            rel = _inside(tree / f, root)
            if rel is None:
                res.notes.append(f"{f}: resolves outside its project {label}, not run")
                continue
            runnable.append(rel)
        if not runnable:
            continue
        if loose:
            label = str(Path(runnable[0]).parent) if label == "." else f"{label}/{Path(runnable[0]).parent}"
        python, err = ensure_test_venv(root, cache_dir, loose_test_deps(root, runnable) if loose else None)
        if python is None:
            res.notes.append(f"{label}: test env did not build: {err[:200]}")
            continue
        stub_version_files(root)
        patched, out_p = _pytest(root, python, runnable, timeout_s)
        if not patched:
            res.notes.append(f"{label}: the patch's tests produced no results on the patched tree")
            res.output += f"\n== patched {label}\n{out_p}"
            continue
        base = base or fresh_base()
        if base is None:
            res.notes.append("no dev tree to compare against")
            break
        base_root = base.resolve() / root_label
        _copy_into(tree, base, material)
        stub_version_files(base_root)
        base_out, out_b = _pytest(base_root, python, runnable, timeout_s)
        prefix = "" if root_label == "." else f"{root_label}/"
        res.patched.update({prefix + k: v for k, v in patched.items()})
        res.base.update({prefix + k: v for k, v in base_out.items()})
        uncollected = sorted(set(runnable) - {k.split("::")[0] for k in base_out})
        if uncollected:
            res.notes.append(f"{label}: {', '.join(uncollected)} couldn't be collected on dev (it likely imports "
                             f"something the patch adds), so it proves nothing there")
        res.output += f"\n== patched {label}\n{out_p}\n== dev {label}\n{out_b}"
    return res


_VITEST_RANK = {"passed": 0, "skipped": 1, "failed": 2}


def parse_vitest_json(data: dict[str, Any], root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in data.get("testResults") or []:
        name = str(f.get("name") or "")
        try:
            rel = str(Path(name).resolve().relative_to(root.resolve()))
        except ValueError:
            rel = name
        for i, a in enumerate(f.get("assertionResults") or []):
            st = str(a.get("status") or "")
            status = "passed" if st == "passed" else "failed" if st == "failed" else "skipped"
            key = f"{rel}::{a.get('fullName') or a.get('title') or f'#{i}'}"
            # vitest allows duplicate names (repeated it() titles, test.each): keep the worst outcome
            if _VITEST_RANK[status] > _VITEST_RANK.get(out.get(key, ""), -1):
                out[key] = status
    return out


def _vitest(root: Path, files: list[str], timeout_s: int) -> tuple[dict[str, str], str]:
    from auditor.validate_node import _run

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        report = Path(fh.name)
    try:
        res = _run([str(root / "node_modules" / ".bin" / "vitest"), "run", "--reporter=json",
                    f"--outputFile={report}", *files], root, timeout_s, tool="vitest")
        try:
            data = json.loads(report.read_text() or "{}")
        except (OSError, ValueError):
            data = {}
        return parse_vitest_json(data, root), res.raw_output
    finally:
        report.unlink(missing_ok=True)


def node_repro(tree: Path, fresh_base: Callable[[], Path | None], tests: list[str], *, cache_dir: Path,
               timeout_s: int = 120) -> ReproResult:
    """Same as :func:`python_repro` for TS/JS test files in npm projects whose test script runs vitest."""
    from auditor.validate_node import _link_modules, ensure_node_modules, node_root

    js_tests = [t for t in tests if _JS_TEST_RE.search(t) and re.search(r"\.(test|spec)\.", t) and (tree / t).is_file()]
    if not js_tests:
        return ReproResult()
    material = [t for t in tests if (tree / t).is_file()]
    res = ReproResult(tool="vitest")
    if shutil.which("node") is None:
        res.notes.append("node not installed: TS/JS tests not run")
        return res
    tree_r = tree.resolve()
    by_root: dict[Path, list[str]] = {}
    for t in js_tests:
        root = node_root(tree, t)
        if root is None:
            res.notes.append(f"{t}: no package.json above it")
            continue
        root = root.resolve()
        if _inside(root, tree_r) is None:
            res.notes.append(f"{t}: its package root is outside the tree, not run")
            continue
        by_root.setdefault(root, []).append(t)
    base = None
    for root, files in sorted(by_root.items()):
        label = _inside(root, tree_r) if root != tree_r else "."
        try:
            script = str((json.loads((root / "package.json").read_text()).get("scripts") or {}).get("test") or "")
        except (OSError, ValueError):
            script = ""
        if "vitest" not in script:
            res.notes.append(f"{label}: its test script isn't vitest, so per-test results aren't read (needs a waiver)")
            continue
        modules, err = ensure_node_modules(root, cache_dir)
        if modules is None:
            res.notes.append(f"{label}: npm ci failed: {err[:200]}")
            continue
        _link_modules(root, modules)
        runnable = []
        for f in files:
            rel = _inside(tree / f, root)
            if rel is None:
                res.notes.append(f"{f}: resolves outside its project {label}, not run")
            else:
                runnable.append(rel)
        if not runnable:
            continue
        patched, out_p = _vitest(root, runnable, timeout_s)
        if not patched:
            res.notes.append(f"{label}: the patch's tests produced no results on the patched tree")
            res.output += f"\n== patched {label}\n{out_p}"
            continue
        base = base or fresh_base()
        if base is None:
            res.notes.append("no dev tree to compare against")
            break
        base_root = base.resolve() / label
        _copy_into(tree, base, material)
        base_modules, _ = ensure_node_modules(base_root, cache_dir)
        if base_modules is None:
            res.notes.append(f"{label}: npm ci failed on the dev tree")
            continue
        _link_modules(base_root, base_modules)
        base_out, out_b = _vitest(base_root, runnable, timeout_s)
        prefix = "" if label == "." else f"{label}/"
        res.patched.update({prefix + k: v for k, v in patched.items()})
        res.base.update({prefix + k: v for k, v in base_out.items()})
        res.output += f"\n== patched {label}\n{out_p}\n== dev {label}\n{out_b}"
    return res


def run_repro(tree: Path, fresh_base: Callable[[], Path | None], tests: list[str], *, venv_cache: Path,
              node_cache: Path, timeout_s: int = 120) -> ReproResult:
    t0 = time.monotonic()
    res = python_repro(tree, fresh_base, tests, cache_dir=venv_cache, timeout_s=timeout_s)
    res.merge(node_repro(tree, fresh_base, tests, cache_dir=node_cache, timeout_s=timeout_s))
    logger.info("repro run: %d test(s), %d repro, %.1fs", len(res.patched), len(res.repro), time.monotonic() - t0)
    return res


# ── the decision ─────────────────────────────────────────────────────────────

@dataclass
class Decision:
    status: str  # "resolved" | "unverified"
    kind: str    # repro | tests | test_only | non_code | structure | waiver | none
    reason: str
    evidence: dict[str, Any]


def tests_ran(tool: str, tests: dict[str, Any] | None = None) -> bool:
    """Something that can fail ran. A pytest run whose known counts (*tests*) show nothing passed is not
    that: every test was skipped or expected to fail. Unknown or partial counts don't say so."""
    parts = set(tool.split("+")) & {"pytest", "npm_checks", "npm_test", "go_test", "cargo_test"}
    if tests and not tests.get("partial") and not tests.get("passed") and not tests.get("xpassed"):
        parts.discard("pytest")
    return bool(parts)


def _nothing_passed(tests: dict[str, Any] | None) -> str:
    """Why a pytest run doesn't count, or "" when it isn't the reason."""
    if not tests or tests.get("partial") or tests.get("passed") or tests.get("xpassed"):
        return ""
    reasons = "; ".join(list(tests.get("skip_reasons") or {})[:3])
    return f"no test passed ({tests.get('skipped', 0)} skipped{': ' + reasons if reasons else ''})"


# File stems too common to tie a test to a pack on their own: ``tests`` importing ``main`` could be
# any project's main. For these the parent directory has to come with it (``repair.models``).
GENERIC_STEMS = frozenset({"main", "app", "server", "utils", "util", "core", "models", "model", "config", "cli",
                           "api", "base", "common", "helpers", "types", "routes", "views", "index", "settings"})


def _module_pattern(pack: dict[str, Any]) -> re.Pattern[str] | None:
    """How a test file names the module the pack cites, or None when the pack cites no file."""
    fp = str((pack.get("location") or {}).get("file_path") or "").replace("\\", "/")
    if not fp:
        return None
    p = PurePosixPath(fp)
    stem, parent = p.stem, p.parent.name
    if stem == "__init__":
        stem, parent = parent, p.parent.parent.name
    if not stem:
        return None
    if stem.lower() in GENERIC_STEMS or len(stem) < 4:
        if not parent:
            return None
        # repair.models, repair/models, from x.repair import models, and the wrapped
        # from x.repair import (\n    models,\n) a formatter writes for a long import list
        par, st = re.escape(parent), re.escape(stem)
        return re.compile(rf"\b{par}[./]{st}\b|^\s*from\s+[\w.]*\b{par}\s+import\s+"
                          rf"(?:[^\n#(]*\b{st}\b|\((?:[^)#]|#[^\n]*\n)*?\b{st}\b)", re.M)
    return re.compile(rf"\b{re.escape(stem)}\b")


def cites_a_file(pack: dict[str, Any]) -> bool:
    return _module_pattern(pack) is not None


def cited_path(pack: dict[str, Any]) -> str:
    return str((pack.get("location") or {}).get("file_path") or "").replace("\\", "/")


def _changes_cited(pack: dict[str, Any], changed: list[str]) -> bool:
    """Whether the patch changes the file the pack cites (its path is absolute in the scan, relative in the patch)."""
    cited = cited_path(pack)
    return any(cited == c or cited.endswith("/" + c.lstrip("/")) for c in changed)


def names_test(wanted: set[str], test_id: str) -> bool:
    """Whether a ``pack_tests`` entry names *test_id*: the exact id, its file, or an id it extends.
    ``t.py::test_y`` names each parametrized case ``t.py::test_y[case1]``, and ``t.py::TestC`` each
    of the class's tests; the match is on whole id parts, so ``t.py::test_y`` never names ``t.py::test_y2``."""
    if test_id in wanted or test_id.split("::", 1)[0] in wanted:
        return True
    base = test_id.split("[", 1)[0]
    parts = base.split("::")
    return any("::".join(parts[:i]) in wanted for i in range(2, len(parts) + 1))


def for_pack(repro: ReproResult, pack: dict[str, Any], tree: Path, *, named: list[str] | None = None) -> ReproResult:
    """The part of one multi-pack repro run that is *pack*'s own evidence (#127).

    A test belongs to the pack when the caller named it for that pack (``pack_tests``: a test id, a
    prefix of one such as a parametrized test without its ``[case]``, or its file), or, with nothing named, when its test file names the module the pack cites. A test
    that fails on dev and passes patched but belongs to another pack is listed in ``scoped_out`` and
    never resolves this one: one patch fixing two things must not let either fix vouch for the other."""
    root = tree.resolve()
    sources: dict[str, str] = {}

    def source(test_id: str) -> str:
        f = test_id.split("::", 1)[0]
        if f not in sources:
            path = (tree / f).resolve()
            try:
                sources[f] = path.read_text(encoding="utf-8", errors="replace") if path.is_relative_to(root) else ""
            except OSError:
                sources[f] = ""
        return sources[f]

    if named:
        wanted = {str(n).strip() for n in named if str(n).strip()}
        keep = {t for t in repro.patched if names_test(wanted, t)}
    else:
        pat = _module_pattern(pack)
        keep = {t for t in repro.patched if pat is not None and pat.search(source(t))}
    return ReproResult(tool=repro.tool, patched={t: s for t, s in repro.patched.items() if t in keep},
                       base={t: s for t, s in repro.base.items() if t in keep}, notes=list(repro.notes),
                       output=repro.output, scoped_out=sorted(set(repro.repro) - keep))


def decide(pack: dict[str, Any], *, tool: str, changed: list[str], repro: ReproResult | None,
           waiver: str = "", base_sha: str = "", structure: dict[str, Any] | None = None,
           tests: dict[str, Any] | None = None) -> Decision:
    """Resolved or unverified, for a validate_patch run that passed. *structure* is
    ``structure.judge``'s verdict; a structural pack without one is never resolved.
    *tests* (``validator.pytest_counts``) is recorded, skips included; a run where nothing passed counts as no run."""
    ch = classify_changes(changed)
    ev: dict[str, Any] = {
        "tool": tool, "base_sha": base_sha, "changed": {k: len(v) for k, v in ch.items()},
        "needs_repro": needs_repro(pack), "finding_type": pack_finding_type(pack),
    }
    if repro is not None:
        ev["repro"] = repro.to_dict()
    if tests:
        ev["tests"] = tests

    def done(status: str, kind: str, reason: str) -> Decision:
        return Decision(status, kind, reason, {**ev, "kind": kind, "reason": reason})

    if pack.get("kind") in STRUCTURE_KINDS:
        # Before the waiver and the docs/test-only shortcuts: none of them says the structure changed.
        verdict = structure or {"gone": False, "reason": "the structure scan didn't run"}
        ev["structure"] = verdict
        if verdict.get("gone") is not True:
            return done("unverified", "none", str(verdict.get("reason") or "the violation is still there"))
        if tests_ran(tool, tests):
            return done("resolved", "structure", f"{verdict.get('reason')}; tests ran and passed")
    if waiver:
        if len(waiver.strip()) < MIN_WAIVER_CHARS:
            return done("unverified", "none", f"evidence_waiver needs a reason of at least {MIN_WAIVER_CHARS} characters")
        ev["waiver"] = waiver.strip()[:1000]
        return done("resolved", "waiver", "evidence waived: " + waiver.strip()[:200])
    if not ch["code"] and not ch["tests"]:
        return done("resolved", "non_code", "docs/config-only change")
    if is_test_file(cited_path(pack)):
        # A finding about a test is fixed by changing that test, and is proven by it running. Another pack's
        # repro in the same diff says nothing about it (argus-puppet's eight test packs were each credited
        # with three code fixes' tests).
        if not _changes_cited(pack, changed):
            return done("unverified", "none", f"the pack cites {cited_path(pack).rsplit('/', 1)[-1]}, which the "
                        "patch doesn't change")
        if tests_ran(tool, tests):
            return done("resolved", "test_only", "the test the pack cites is changed, and the tests ran and passed")
        return done("unverified", "none", "the cited test is changed, but " + (_nothing_passed(tests) or
                                                                                 "no tests ran (lint only)"))
    if not ch["code"]:
        if tests_ran(tool, tests):
            return done("resolved", "test_only", "test-only change; the tests ran and passed")
        return done("unverified", "none", "test-only change, but " + (_nothing_passed(tests) or "no tests ran (lint only)"))
    if ev["needs_repro"]:
        if repro is not None and repro.repro:
            return done("resolved", "repro", f"{len(repro.repro)} test(s) fail on dev and pass with the fix: "
                        + ", ".join(repro.repro[:5]))
        if not ch["tests"]:
            return done("unverified", "none", "no test added or changed: add one that fails on dev and passes "
                        "with the fix, or pass evidence_waiver")
        if repro is not None and repro.scoped_out:
            return done("unverified", "none", f"{len(repro.scoped_out)} test(s) fail on dev and pass with the fix, "
                        "but none is this pack's own: their test files don't name the pack's module, or they "
                        "aren't among the tests pack_tests names for it. Name this pack's tests in pack_tests")
        if repro is None or not repro.ran:
            why = "; ".join(repro.notes) if repro and repro.notes else "the patch's tests didn't run"
            return done("unverified", "none", f"couldn't run the patch's tests on both trees: {why}")
        return done("unverified", "none", "none of the patch's tests fails on dev, so they don't show the bug: "
                    "make a test fail without the fix")
    if tests_ran(tool, tests):
        return done("resolved", "tests", "tests ran and passed")
    return done("unverified", "none", _nothing_passed(tests) or "only lint ran: no tests or npm checks cover this change")
