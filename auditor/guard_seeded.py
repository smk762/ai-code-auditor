"""Seeded-defect recall for class guards (#63): does each proven guard catch its own class?

A class guard (``auditor/class_packs.py``) is proven when its proof test passes on ``dev`` and
fails on the tree just before it. That shows the guard does *something*, not that it catches the
class. Guards without a static rule (a helper plus a test, like agent-composer's ssrf and info-leak
guards) close no children, so nothing else measures them. This eval does, for every guard:

1. **Export** the mirror's ``dev`` to a scratch dir (never the mirror itself).
2. **Baseline.** Run the guard's proof test on the clean export with a tracer that records which
   files the test opens for reading while its tests run (imports don't count). If the test already
   fails, the guard is ``invalid``: nothing it does to a seed would mean anything.
3. **Scope.** Those files, minus tests, fixtures and anything that doesn't parse, are the guard's
   own scope: the code its repo-wide check actually reads. A guard with a static rule and no traced
   reads falls back to every non-test ``.py`` in the repo.
4. **Plant**, one seed at a time, each seed of the guard's class (``config/guard_seeds.yaml``) into
   a file chosen from that scope (seeded by ISO week, so each week is reproducible and the files
   rotate). The seed's imports go after the file's last top-level import and its code at the end.
5. **Check.** The guard's rule (ruff, noqa ignored) must hit a line of the seed, or its proof test
   must pass on the clean export and fail on the planted one (``class_packs.prove``'s runner, with
   the planted tree as the "before" side). Either is ``caught``; neither is ``missed``. A check that
   can't run (no ruff, no test results, a clean test that fails) makes the seed ``invalid``, which
   is never counted as caught or missed.

A seed may also carry **variants** (#155), planted at its site and kept apart from the seeds, so
``guards.seeded_recall`` means what it always did:

* ``dodges``: the same defect rewritten so a shallow check may lose it. The guard must still fire. A
  dodge counts only when its seed was caught; a guard blind to the seed wasn't dodged.
* ``fixes``: the defect is gone. The guard must stay silent; firing is a false alarm.

They feed the informational gates ``guards.dodge_recall`` and ``guards.fix_silence``.

Results go to ``reports/guard_seeded.json``; the ``guards.seeded_recall`` gate in
``auditor/trust_eval.py`` reads them. The gate counts only guards whose proof was ``proven``.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import random
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from auditor.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

RESULT_NAME = "guard_seeded.json"
SEEDS_PATH = Path(__file__).resolve().parents[1] / "config" / "guard_seeds.yaml"
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "fixtures", "build", "dist"}


@dataclass(frozen=True)
class Seed:
    id: str
    cls: str
    code: str
    imports: tuple[str, ...] = ()
    why: str = ""
    dodges: tuple[Seed, ...] = ()
    fixes: tuple[Seed, ...] = ()

    @property
    def marker(self) -> str:
        return f"_seeded_{self.id}"


def load_seeds(path: str | Path = SEEDS_PATH) -> dict[str, list[Seed]]:
    """Seeds by class. A seed whose code doesn't define ``_seeded_<id>`` or doesn't parse is an
    error: a template that can't be located in the planted file would read as a miss."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, list[Seed]] = {}
    seen: set[str] = set()

    def load(cls: str, it: dict[str, Any], *, variant: bool = False) -> Seed:
        if variant and (it.get("dodges") or it.get("fixes")):
            raise ValueError(f"variant {it['id']} can't have variants of its own")
        s = Seed(id=str(it["id"]), cls=cls, code=str(it["code"]).rstrip() + "\n",
                 imports=tuple(str(x) for x in it.get("imports") or ()), why=str(it.get("why") or ""),
                 dodges=tuple(load(cls, v, variant=True) for v in it.get("dodges") or ()),
                 fixes=tuple(load(cls, v, variant=True) for v in it.get("fixes") or ()))
        if s.id in seen:
            raise ValueError(f"duplicate seed id {s.id}")
        seen.add(s.id)
        tree = ast.parse("\n".join(s.imports) + "\n" + s.code)
        if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == s.marker for n in tree.body):
            raise ValueError(f"seed {s.id} must define {s.marker}")
        return s

    for cls, items in raw.items():
        for it in items or []:
            out.setdefault(str(cls), []).append(load(str(cls), it))
    return out


# ── planting ─────────────────────────────────────────────────────────────────

def plant(source: str, seed: Seed) -> tuple[str, int, int]:
    """*source* with *seed* planted, and the 1-based first and last line of the seed's code."""
    lines = source.splitlines()
    tree = ast.parse(source)
    at = 0
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if imports:
        at = max(n.end_lineno or n.lineno for n in imports)
    elif (tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant)
          and isinstance(tree.body[0].value.value, str)):
        at = tree.body[0].end_lineno or 1  # after the module docstring
    lines[at:at] = list(seed.imports)
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["", ""]
    start = len(lines) + 1
    lines += seed.code.rstrip("\n").splitlines()
    return "\n".join(lines) + "\n", start, len(lines)


def _plantable(tree: Path, rel: str, exclude: set[str]) -> bool:
    from auditor.evidence import is_test_file

    p = Path(rel)
    if p.suffix != ".py" or rel in exclude or is_test_file(rel) or p.name == "conftest.py":
        return False
    if _SKIP_DIRS.intersection(p.parts[:-1]) or any(part.startswith(".") for part in p.parts[:-1]):
        return False
    f = tree / rel
    if f.is_symlink() or not f.is_file():
        return False
    try:
        ast.parse(f.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return False
    return True


def plant_sites(tree: Path, traced: list[str], *, rule: dict[str, Any] | None, exclude: set[str]) -> tuple[list[str], str]:
    """Files a seed may go in, and where they came from (``traced`` or ``repo``)."""
    sites = sorted({r for r in traced if _plantable(tree, r, exclude)})
    if sites:
        return sites, "traced"
    if rule:  # a static rule covers every .py; the proof test may only read its fixtures
        root = tree.resolve()
        every = (str(p.relative_to(root)) for p in root.rglob("*.py"))
        return sorted(r for r in every if _plantable(tree, r, exclude)), "repo"
    return [], ""


# ── tracing the proof test's scope ───────────────────────────────────────────

_TRACER = '''\
"""pytest plugin (guard_seeded): record files opened for reading while tests run."""
import builtins, io, json, os
import pytest

_ROOT = os.path.realpath(os.environ["GUARD_TRACE_ROOT"])
_OUT = os.environ["GUARD_TRACE_OUT"]
_seen = set()
_on = [False]
_orig = io.open


def _open(file, mode="r", *args, **kwargs):
    if _on[0] and not any(c in str(mode) for c in "wax+"):
        try:
            p = os.fsdecode(os.path.realpath(os.fspath(file)))
        except (TypeError, ValueError):  # a file descriptor, or a path that can't be decoded
            p = ""
        if p.startswith(_ROOT + os.sep):
            _seen.add(os.path.relpath(p, _ROOT))
    return _orig(file, mode, *args, **kwargs)


io.open = builtins.open = _open


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _on[0] = True
    try:
        yield
    finally:
        _on[0] = False


def pytest_unconfigure(config):
    with _orig(_OUT, "w") as fh:
        json.dump(sorted(_seen), fh)
'''


@dataclass
class Trace:
    outcomes: dict[str, str] = field(default_factory=dict)  # test id (tree-relative) -> outcome
    files: list[str] = field(default_factory=list)          # tree-relative files read during tests
    note: str = ""


Tracer = Callable[[Path, str], Trace]


def trace_proof(tree: Path, proof_test: str, *, venv_cache: Path, timeout_s: int = 180) -> Trace:
    """Run *proof_test* on *tree* once, recording its outcomes and the files its tests read."""
    from auditor.evidence import parse_pytest_outcomes
    from auditor.proc import child_env
    from auditor.proc import run as proc_run
    from auditor.validator import (
        _has_project,
        ensure_test_venv,
        loose_test_deps,
        project_root,
        stub_version_files,
    )

    if not proof_test.endswith(".py"):
        return Trace(note="the proof test isn't Python, so its scope can't be traced")
    tree_r = tree.resolve()
    root = project_root(tree, proof_test).resolve()
    rel = str((tree / proof_test).resolve().relative_to(root))
    python, err = ensure_test_venv(root, venv_cache, None if _has_project(root) else loose_test_deps(root, [rel]))
    if python is None:
        return Trace(note=f"test env did not build: {err[:200]}")
    stub_version_files(root)
    with tempfile.TemporaryDirectory(prefix="guard-trace-") as tmp:
        (Path(tmp) / "guard_scope_trace.py").write_text(_TRACER, encoding="utf-8")
        out_file = Path(tmp) / "read.json"
        paths = [str(root / "src")] if (root / "src").is_dir() else []
        env = child_env(PYTHONPATH=os.pathsep.join([tmp, *paths, str(root)]), PYTHONDONTWRITEBYTECODE="1",
                        GUARD_TRACE_ROOT=str(tree_r), GUARD_TRACE_OUT=str(out_file))
        try:
            proc = proc_run([str(python), "-m", "pytest", "-q", "-rA", "--tb=line", "-p", "no:cacheprovider",
                             "-p", "guard_scope_trace", "--", rel],
                            cwd=root, env=env, capture_output=True, text=True, timeout=timeout_s, check=False)
        except Exception as exc:  # noqa: BLE001 — a timeout or spawn failure is an invalid baseline, reported
            return Trace(note=f"proof test did not run: {str(exc)[:200]}")
        try:
            files = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            files = []
    prefix = "" if root == tree_r else f"{root.relative_to(tree_r)}/"
    outcomes = {prefix + k: v for k, v in parse_pytest_outcomes(proc.stdout + proc.stderr).items()}
    note = "" if outcomes else f"no results: {(proc.stdout + proc.stderr)[-300:]}"
    return Trace(outcomes=outcomes, files=files, note=note)


# ── the run ──────────────────────────────────────────────────────────────────

@dataclass
class SeedResult:
    seed: str
    file: str = ""
    status: str = ""     # caught | missed | invalid
    rule: str = ""       # caught | missed | error | "" (the guard has no rule)
    test: str = ""       # caught | missed | invalid
    caught_by: list[str] = field(default_factory=list)  # rule codes and test ids that fired on the seed
    lines: list[int] = field(default_factory=list)
    note: str = ""
    code: str = ""
    of: str = ""         # for a variant: the seed it rewrites


@dataclass
class GuardResult:
    pack: str
    repo: str
    cls: str
    proof_status: str
    commit: str
    dev_sha: str = ""
    status: str = ""     # ok | invalid
    scope: str = ""      # traced | repo
    sites: int = 0
    note: str = ""
    seeds: list[SeedResult] = field(default_factory=list)
    dodges: list[SeedResult] = field(default_factory=list)  # caught is right
    fixes: list[SeedResult] = field(default_factory=list)   # missed (silent) is right


def proven_guards(packs_from: Path, statuses: tuple[str, ...] = ("proven",)) -> list[tuple[str, str, str, dict]]:
    """``(pack id, class, repo, guard record)`` for each recorded guard whose proof is in *statuses*."""
    from auditor.agent_pack import _read_pack_yaml, packs_dir
    from auditor.class_packs import KIND

    out = []
    for p in sorted(packs_dir(packs_from).glob("pack-class-*.yaml")):
        d = _read_pack_yaml(p) or {}
        if d.get("kind") != KIND:
            continue
        gov = d.get("governance")
        guards = gov.get("class_guard") if isinstance(gov, dict) else None
        if not isinstance(guards, dict):
            continue
        cls = str(gov.get("class_id") or "")
        pid = str(d.get("work_item_id") or p.stem)
        for repo, rec in sorted(guards.items()):
            proof = rec.get("proof") if isinstance(rec, dict) else None
            if isinstance(proof, dict) and proof.get("status") in statuses:
                out.append((pid, cls, str(repo), rec))
    return out


def eval_guard(pack: str, cls: str, repo: str, rec: dict[str, Any], mirror: Path | None, seeds: list[Seed], *,
               runner: Any, tracer: Tracer, rng: random.Random, rule_runner: Any = None) -> GuardResult:
    from auditor.class_packs import _export, rule_hits
    from auditor.pack_gc import Mirror

    rule_runner = rule_runner or rule_hits
    proof = rec.get("proof") or {}
    g = GuardResult(pack=pack, repo=repo, cls=cls, proof_status=str(proof.get("status") or ""),
                    commit=str(rec.get("commit") or ""))
    m = Mirror(mirror)
    if not m.ok or m.path is None:
        g.status, g.note = "invalid", "no mirror for this repo"
        return g
    if not seeds:
        g.status, g.note = "invalid", f"no seeds for class {cls} (config/guard_seeds.yaml)"
        return g
    g.dev_sha = m._git("rev-parse", m.ref).stdout.strip()
    if not g.commit or m._git("merge-base", "--is-ancestor", g.commit, m.ref).returncode != 0:
        g.status, g.note = "invalid", f"the guard commit isn't on the mirror's {m.ref} yet"
        return g
    proof_test = str(rec.get("proof_test") or "")
    fixtures = [str(f) for f in rec.get("fixtures") or []]
    tests = [proof_test, *fixtures]
    rule = rec.get("rule") or None
    with tempfile.TemporaryDirectory(prefix=f"guard-seeded-{repo}-") as tmp:
        clean = Path(tmp) / "clean"
        if not _export(m.path, g.dev_sha, clean):
            g.status, g.note = "invalid", "could not export dev"
            return g
        root = clean.resolve()
        if any(Path(t).is_absolute() or not (clean / t).resolve().is_relative_to(root) for t in tests):
            g.status, g.note = "invalid", "proof test or fixtures outside the repo"
            return g
        if not (clean / proof_test).is_file():
            g.status, g.note = "invalid", f"proof test not on dev: {proof_test}"
            return g
        base = tracer(clean, proof_test)
        failing = sorted(t for t, s in base.outcomes.items() if s not in ("passed", "skipped"))
        if not base.outcomes or failing:
            g.status = "invalid"
            g.note = (f"the check already fails on the unplanted export: {', '.join(failing[:5])}" if failing
                      else f"the proof test produced no results on the unplanted export {base.note}".strip())
            return g
        sites, g.scope = plant_sites(clean, base.files, rule=rule, exclude={proof_test, *fixtures})
        g.sites = len(sites)
        if not sites:
            g.status, g.note = "invalid", "no file in the guard's scope to plant in (its proof test read no source)"
            return g
        g.status = "ok"
        for seed in seeds:
            site = rng.choice(sites)

            def check(s: Seed, at: str = site) -> SeedResult:
                return _check_seed(s, clean, Path(tmp), at, tests, rule, runner, rule_runner)
            base_result = check(seed)
            g.seeds.append(base_result)
            for variant in seed.dodges:
                if base_result.status == "caught":
                    r = check(variant)
                else:
                    r = SeedResult(seed=variant.id, file=site, status="invalid",
                                   note=f"its seed {seed.id} was {base_result.status}, so nothing was dodged")
                r.of = seed.id
                g.dodges.append(r)
            for variant in seed.fixes:
                r = check(variant)
                r.of = seed.id
                g.fixes.append(r)
    return g


def _through_symlink(root: Path, rel: str) -> bool:
    """Whether *rel* under *root*, or a directory between them, is a symlink, or resolves outside
    *root*."""
    p = root
    for part in Path(rel).parts:
        p = p / part
        if p.is_symlink():
            return True
    return not (root / rel).resolve().is_relative_to(root.resolve())


def _check_seed(seed: Seed, clean: Path, work: Path, site: str, tests: list[str], rule: dict[str, Any] | None,
                runner: Any, rule_runner: Any) -> SeedResult:
    r = SeedResult(seed=seed.id, file=site, code="\n".join([*seed.imports, "...", seed.code.rstrip()]))
    if _through_symlink(clean, site):
        # reading or writing it would follow the link, possibly out of the export (into the mirror)
        r.status, r.note = "invalid", f"the plant site {site} is (or is under) a symlink"
        return r
    src = (clean / site).read_text(encoding="utf-8")
    planted_src, first, last = plant(src, seed)
    r.lines = [first, last]
    try:
        ast.parse(planted_src)
    except SyntaxError as exc:
        r.status, r.note = "invalid", f"planting broke the file: {exc}"
        return r
    rule_caught = False
    if rule:
        hits = rule_runner(planted_src, site, rule)
        if hits is None:
            r.rule = "error"
        else:
            on_seed = sorted({h.code for h in hits if first <= h.line <= last})
            rule_caught = bool(on_seed)
            r.rule = "caught" if rule_caught else "missed"
            r.caught_by += [f"{rule.get('tool')}:{c}" for c in on_seed]

    planted = work / f"planted-{seed.id}"

    def fresh() -> Path | None:
        shutil.rmtree(planted, ignore_errors=True)
        shutil.copytree(clean, planted, symlinks=True)
        if _through_symlink(planted, site):  # copytree keeps links: never write through one
            raise ValueError(f"refusing to plant through a symlink: {site}")
        (planted / site).write_text(planted_src, encoding="utf-8")
        return planted

    # The runner's "patched" side is the clean export and its "before" side the planted one, so
    # ``repro`` is exactly the tests that pass clean and fail with the seed.
    try:
        res = runner(clean, fresh, tests)
    finally:
        shutil.rmtree(planted, ignore_errors=True)
    clean_out = getattr(res, "patched", {}) or {}
    failing = sorted(t for t, s in clean_out.items() if s not in ("passed", "skipped"))
    if not clean_out or failing:
        r.test = "invalid"
        r.note = f"the proof test failed on the clean export: {', '.join(failing[:3])}" if failing else \
            "the proof test produced no results"
    else:
        repro = list(getattr(res, "repro", []) or [])
        r.test = "caught" if repro else "missed"
        r.caught_by += repro
    if r.test == "invalid" or r.rule == "error":
        r.status = "invalid"
        r.note = r.note or f"couldn't run the guard rule ({(rule or {}).get('tool')})"
    else:
        r.status = "caught" if rule_caught or r.test == "caught" else "missed"
    return r


def run(output_dir: str | Path | None = None, *, packs_from: str | Path | None = None,
        mirrors_path: str | Path = "config/repo_mirrors.yaml", seeds_path: str | Path = SEEDS_PATH,
        statuses: tuple[str, ...] = ("proven",), runner: Any = None, tracer: Tracer | None = None,
        rule_runner: Any = None, seed: str | None = None) -> dict[str, Any]:
    """Plant every seed for each guard's class and record what its check caught."""
    from auditor.class_packs import _default_runner
    from auditor.pack_gc import mirror_paths

    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    y, w, _ = now.isocalendar()
    seed = seed or f"{y}-W{w:02d}"
    venv = Path(os.getenv("AI_AUDIT_VALIDATE_VENV_DIR") or out / "_validate_venvs")
    runner = runner or _default_runner(out)
    tracer = tracer or (lambda tree, test: trace_proof(tree, test, venv_cache=venv))
    seeds = load_seeds(seeds_path)
    mpaths = mirror_paths(mirrors_path)
    t0 = time.monotonic()
    guards: list[GuardResult] = []
    for pack, cls, repo, rec in proven_guards(Path(packs_from or out), statuses):
        rng = random.Random(f"{seed}:{pack}:{repo}")
        try:
            guards.append(eval_guard(pack, cls, repo, rec, mpaths.get(repo), seeds.get(cls, []), runner=runner,
                                     tracer=tracer, rng=rng, rule_runner=rule_runner))
        except Exception as exc:  # noqa: BLE001 — one guard's breakage is recorded, not fatal to the eval
            logger.warning("guard seeded eval: %s %s failed: %s", pack, repo, exc)
            guards.append(GuardResult(pack=pack, repo=repo, cls=cls, proof_status=str((rec.get("proof") or {})
                                      .get("status") or ""), commit=str(rec.get("commit") or ""), status="invalid",
                                      note=f"eval crashed: {str(exc)[:300]}"))
    report = {"at": now.isoformat(), "seed": seed, "seconds": round(time.monotonic() - t0, 1),
              "summary": summarize(guards), "guards": [asdict(g) for g in guards]}
    report["variants"] = {k: {x: v for x, v in _variant_counts(report["guards"], k).items() if x != "wrong"}
                          for k in ("dodges", "fixes")}
    atomic_write_text(out / RESULT_NAME, json.dumps(report, indent=1))
    return report


def summarize(guards: list[GuardResult]) -> dict[str, Any]:
    s = [x for g in guards for x in g.seeds]
    return {"guards": len(guards), "planted": len(s), "caught": sum(x.status == "caught" for x in s),
            "missed": sum(x.status == "missed" for x in s), "invalid": sum(x.status == "invalid" for x in s)}


def recall_metric(result: dict[str, Any] | None) -> dict[str, Any]:
    """For the ``guards.seeded_recall`` gate: caught / (caught + missed) over the seeds of guards
    whose proof was ``proven``. ``rate`` is None when there are no proven guards, or when any proven
    guard has fewer than two valid seeds (an invalid guard must not drop out of the denominator)."""
    guards = [g for g in (result or {}).get("guards") or [] if g.get("proof_status") == "proven"]
    seeds = [s for g in guards for s in g.get("seeds") or [] if s.get("status") in ("caught", "missed")]
    caught = sum(s["status"] == "caught" for s in seeds)
    short = [f"{g['pack']}@{g['repo']}" for g in guards
             if sum(s.get("status") in ("caught", "missed") for s in g.get("seeds") or []) < 2]
    missed = [f"{g['pack']}@{g['repo']}:{s['seed']}" for g in guards for s in g.get("seeds") or []
              if s.get("status") == "missed"]
    note = ("no proven guards" if not guards else
            f"too few valid seeds: {', '.join(short)}" if short else
            f"missed: {', '.join(missed[:6])}" if missed else "")
    rate = round(caught / len(seeds), 4) if guards and seeds and not short else None
    return {"guards": len(guards), "n": len(seeds), "caught": caught, "rate": rate, "note": note}


def _variant_counts(guards: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    rows = [(g, s) for g in guards for s in g.get(kind) or []]
    valid = [(g, s) for g, s in rows if s.get("status") in ("caught", "missed")]
    right = "caught" if kind == "dodges" else "missed"
    wrong = [f"{g['pack']}@{g['repo']}:{s['seed']}" for g, s in valid if s["status"] != right]
    return {"n": len(valid), "right": len(valid) - len(wrong), "invalid": len(rows) - len(valid), "wrong": wrong}


def variant_metric(result: dict[str, Any] | None, kind: str) -> dict[str, Any]:
    """For ``guards.dodge_recall`` (*kind* ``dodges``: the share caught) and ``guards.fix_silence``
    (``fixes``: the share the guard stays silent on), over proven guards. Invalid ones aren't counted."""
    guards = [g for g in (result or {}).get("guards") or [] if g.get("proof_status") == "proven"]
    c = _variant_counts(guards, kind)
    what = "dodged by" if kind == "dodges" else "fired on"
    note = (f"{what}: {', '.join(c['wrong'][:6])}" if c["wrong"] else f"no valid {kind}" if not c["n"] else "")
    if c["invalid"]:
        note = "; ".join(x for x in (note, f"{c['invalid']} invalid") if x)
    return {"n": c["n"], "rate": round(c["right"] / c["n"], 4) if c["n"] else None, "note": note}
