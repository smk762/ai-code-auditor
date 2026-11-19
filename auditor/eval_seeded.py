"""Seeded-defect eval for the evidence bar (#63, #58): does it hold on the suite's real repos?

The unit tests prove the bar's logic on small synthetic trees. This eval checks that its
machinery works on the real projects agents fix: their layouts, their deps and venvs, their
imports and their test suites.

For a sample of Python test files in the suite's mirrors (``origin/dev``), it seeds a real bug
into a function the test imports. The bug is one AST operator flip, such as ``==`` → ``!=``,
``+`` → ``-``, ``and`` → ``or`` or ``True`` → ``False``, and it is kept only when the real test
catches it (a test passes on clean code and fails on the mutant). Two cases are then put to the
bar:

- **fix** (``dev`` = the mutant, patch = undo the bug and touch the test): the bar must resolve
  with ``repro`` evidence naming a test that killed the mutant.
- **control** (``dev`` = the mutant, patch = a cosmetic comment in the module and the same test):
  the bar must *not* resolve. A test that fails on both trees proves nothing.

The kill check runs the test directly and the bar runs it through ``evidence.python_repro``, so
the two are independent paths to the same answer. Results go to ``reports/eval_seeded.json``,
which the trust eval gates read. The sample rotates weekly (seeded by ISO week), so coverage grows
over time while each week's run is reproducible.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import random
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auditor.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

SEEDED_NAME = "eval_seeded.json"
BUG_PACK = {"kind": "nightly", "narrative": {"finding_type": "bug"}}
_SWAP_CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE,
             ast.LtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_SWAP_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv}
_SWAP_BOOL = {ast.And: ast.Or, ast.Or: ast.And}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


@dataclass
class Scenario:
    case: str          # fix | control
    repo: str
    test: str          # repo-relative test file
    module: str        # repo-relative module holding the seeded bug
    mutation: str      # e.g. "calc.add L12: Eq → NotEq"
    killed: list[str] = field(default_factory=list)  # tests that catch the bug (direct run)
    repro: list[str] = field(default_factory=list)   # tests the bar found failing on dev, passing patched
    status: str = ""   # the bar's decision: resolved | unverified
    kind: str = ""     # evidence kind
    detected: bool = False  # fix: bar proved it; control: bar (wrongly) accepted it
    seconds: float = 0.0


# ── mutation ─────────────────────────────────────────────────────────────────

def _byte_offsets(src: bytes) -> list[int]:
    starts, pos = [0], 0
    for line in src.splitlines(keepends=True):
        pos += len(line)
        starts.append(pos)
    return starts


def mutants(source: str, func: str) -> list[tuple[str, str]]:
    """``(description, mutated source)`` for each single-operator flip inside top-level *func*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func), None)
    if fn is None:
        return []
    raw = source.encode()
    starts = _byte_offsets(raw)
    out: list[tuple[str, str]] = []
    for node in ast.walk(fn):
        new: ast.AST | None = None
        desc = ""
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _SWAP_CMP:
            to = _SWAP_CMP[type(node.ops[0])]
            new = ast.Compare(left=node.left, ops=[to()], comparators=node.comparators)
            desc = f"{type(node.ops[0]).__name__} → {to.__name__}"
        elif isinstance(node, ast.BinOp) and type(node.op) in _SWAP_BIN:
            to = _SWAP_BIN[type(node.op)]
            new = ast.BinOp(left=node.left, op=to(), right=node.right)
            desc = f"{type(node.op).__name__} → {to.__name__}"
        elif isinstance(node, ast.BoolOp) and type(node.op) in _SWAP_BOOL:
            to = _SWAP_BOOL[type(node.op)]
            new = ast.BoolOp(op=to(), values=node.values)
            desc = f"{type(node.op).__name__} → {to.__name__}"
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool):
            node = node.value
            new = ast.Constant(value=not node.value)
            desc = f"return {node.value} → {not node.value}"
        if new is None or getattr(node, "end_lineno", None) is None:
            continue
        a = starts[node.lineno - 1] + node.col_offset
        b = starts[node.end_lineno - 1] + node.end_col_offset
        text = ast.unparse(new)
        if "\n" in raw[a:b].decode(errors="replace"):
            text = f"({text})"
        out.append((f"{func} L{node.lineno}: {desc}", (raw[:a] + text.encode() + raw[b:]).decode()))
    return out


# ── finding test → module targets ────────────────────────────────────────────

def targets(root: Path, test: Path) -> list[tuple[Path, str]]:
    """``(module file, function)`` pairs a test imports with ``from pkg.mod import f``."""
    try:
        tree = ast.parse(test.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
            continue
        rel = node.module.replace(".", "/")
        mod = next((p for p in (root / "src" / f"{rel}.py", root / f"{rel}.py") if p.is_file()), None)
        if mod is None:
            continue
        try:
            defined = {n.name for n in ast.parse(mod.read_text(encoding="utf-8")).body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        out += [(mod, a.name) for a in node.names if a.name in defined]
    return out


# ── running ──────────────────────────────────────────────────────────────────

def _export(mirror: Path, dest: Path) -> bool:
    """The mirror's committed ``origin/dev`` (else HEAD) as a plain tree, like validate_patch's base."""
    for ref in ("origin/dev", "HEAD"):
        r = subprocess.run(["git", "archive", "--format=tar", ref], cwd=mirror, capture_output=True, timeout=300,
                           check=False)
        if r.returncode == 0:
            dest.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile() as fh:
                fh.write(r.stdout)
                fh.seek(0)
                with tarfile.open(fileobj=fh) as tar:
                    tar.extractall(dest, filter="data")
            return True
    return False


def _tests(tree: Path) -> list[Path]:
    return sorted(p for p in tree.rglob("test_*.py")
                  if not any(part in _SKIP_DIRS for part in p.relative_to(tree).parts))


def _passing(outcomes: dict[str, str]) -> set[str]:
    return {t for t, s in outcomes.items() if s == "passed"}


def scenarios_for_repo(name: str, mirror: Path, work: Path, *, rng: random.Random, want: int, venv_cache: Path,
                       timeout_s: int, budget_s: float) -> list[Scenario]:
    from auditor import evidence
    from auditor.validator import ensure_test_venv, project_root, stub_version_files

    t_start = time.monotonic()
    clean = work / "clean"
    if not _export(mirror, clean):
        return []
    tests = _tests(clean)
    rng.shuffle(tests)
    found: list[Scenario] = []
    for test in tests:
        if len(found) >= 2 * want or time.monotonic() - t_start > budget_s:
            break
        root = project_root(clean, str(test.relative_to(clean)))
        cands = targets(root, test)
        if not cands:
            continue
        python, _err = ensure_test_venv(root, venv_cache)
        if python is None:
            continue
        stub_version_files(root)
        t_rel = str(test.relative_to(root))
        base_out, _ = evidence._pytest(root, python, [t_rel], timeout_s)
        if not _passing(base_out):
            continue
        rng.shuffle(cands)
        for mod, fn in cands[:4]:
            hit = _kill(clean, root, mod, fn, t_rel, python, timeout_s, work, rng)
            if hit is None:
                continue
            desc, mutated_src, killed = hit
            found += _judge_pair(name, clean, root, test, mod, desc, mutated_src, killed, work, venv_cache, timeout_s)
            break
    return found


def _kill(clean: Path, root: Path, mod: Path, fn: str, t_rel: str, python: Path, timeout_s: int, work: Path,
          rng: random.Random) -> tuple[str, str, list[str]] | None:
    """The first operator flip in *fn* that the test catches: (description, source, killing tests)."""
    from auditor import evidence

    original_bytes = mod.read_bytes()  # restored byte for byte: read_text would rewrite CRLF endings
    original = original_bytes.decode("utf-8")
    ms = mutants(original, fn)
    rng.shuffle(ms)
    clean_out, _ = evidence._pytest(root, python, [t_rel], timeout_s)
    for desc, src in ms[:3]:
        try:
            mod.write_text(src, encoding="utf-8")
            out, _ = evidence._pytest(root, python, [t_rel], timeout_s)
        finally:
            mod.write_bytes(original_bytes)
        killed = sorted(t for t in _passing(clean_out) if out.get(t) in ("failed", "error"))
        if killed:
            return desc, src, killed
    return None


def _judge_pair(name: str, clean: Path, root: Path, test: Path, mod: Path, desc: str, mutated_src: str,
                killed: list[str], work: Path, venv_cache: Path, timeout_s: int) -> list[Scenario]:
    from auditor import evidence

    rel_test = str(test.relative_to(clean))
    rel_mod = str(mod.relative_to(clean))
    prefix = str(root.relative_to(clean))
    killed_ids = [t if prefix == "." else f"{prefix}/{t}" for t in killed]

    def mutant_tree(tag: str) -> Path:
        d = work / f"mutant-{tag}"
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(clean, d, symlinks=True)
        (d / rel_mod).write_text(mutated_src, encoding="utf-8")
        return d

    out = []
    for case in ("fix", "control"):
        t0 = time.monotonic()
        if case == "fix":
            patched = clean  # the fix restores the original code
        else:
            patched = mutant_tree("control-patched")
            (patched / rel_mod).write_text(mutated_src + "\n# cosmetic change, not a fix\n", encoding="utf-8")
        res = evidence.python_repro(patched, lambda case=case: mutant_tree(f"{case}-base"), [rel_test],
                                    cache_dir=venv_cache, timeout_s=timeout_s)
        d = evidence.decide(BUG_PACK, tool="pytest", changed=[rel_mod, rel_test], repro=res)
        proven = d.status == "resolved" and d.kind == "repro"
        detected = proven and bool(set(res.repro) & set(killed_ids)) if case == "fix" else d.status == "resolved"
        out.append(Scenario(case=case, repo=name, test=rel_test, module=rel_mod, mutation=desc, killed=killed_ids,
                            repro=res.repro, status=d.status, kind=d.kind, detected=detected,
                            seconds=round(time.monotonic() - t0, 1)))
    return out


def run(output_dir: str | Path | None = None, *, repos: list[str] | None = None, want_per_repo: int = 1,
        max_repos: int = 6, timeout_s: int = 180, budget_s_per_repo: float = 300,
        mirrors_path: str | Path = "config/repo_mirrors.yaml", seed: str | None = None) -> dict[str, Any]:
    """Seed bugs into up to *max_repos* repos and record how the evidence bar judged them."""
    from auditor.trust_metrics import _mirror_paths

    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    now = datetime.now(UTC)
    y, w, _ = now.isocalendar()
    seed = seed or f"{y}-W{w:02d}"
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)  # before the long run, so the report can always be written
    venv_cache = Path(os.getenv("AI_AUDIT_VALIDATE_VENV_DIR") or out / "_validate_venvs")
    mirrors = {k: v for k, v in _mirror_paths(mirrors_path).items() if (v / ".git").exists()}
    names = sorted(repos or mirrors)
    rng.shuffle(names)
    scenarios: list[Scenario] = []
    tried: list[str] = []
    t0 = time.monotonic()
    for name in names:
        if len({s.repo for s in scenarios}) >= max_repos:
            break
        if name not in mirrors:
            continue
        tried.append(name)
        with tempfile.TemporaryDirectory(prefix=f"eval-seeded-{name}-") as tmp:
            try:
                scenarios += scenarios_for_repo(name, mirrors[name], Path(tmp), rng=rng, want=want_per_repo,
                                                venv_cache=venv_cache, timeout_s=timeout_s,
                                                budget_s=budget_s_per_repo)
            except Exception as exc:  # noqa: BLE001 — one repo's breakage doesn't end the eval
                logger.warning("seeded eval: %s failed: %s", name, exc)
    fixes = [s for s in scenarios if s.case == "fix"]
    controls = [s for s in scenarios if s.case == "control"]
    report = {
        "at": now.isoformat(), "seed": seed, "repos_tried": tried,
        "repos_with_scenarios": sorted({s.repo for s in scenarios}),
        "fix": {"n": len(fixes), "detected": sum(s.detected for s in fixes)},
        "control": {"n": len(controls), "wrongly_accepted": sum(s.detected for s in controls)},
        "seconds": round(time.monotonic() - t0, 1),
        "scenarios": [asdict(s) for s in scenarios],
    }
    atomic_write_text(out / SEEDED_NAME, json.dumps(report, indent=1))
    return report
