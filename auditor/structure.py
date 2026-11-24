"""Structure inside a repo (#154): module cycles and private imports. Report-only, no LLM.

``arch_contracts`` covers repo-to-repo imports; nothing looked inside a repo. Two checks, Python only:

* ``module-cycle``: modules that use each other in a circle, one entry per cycle (a strongly connected
  component), keyed by its sorted member paths, never by line. It can't be hidden cosmetically:
  an import inside a function counts (it is marked ``lazy``), and so do ``import_module("…")`` and
  ``__import__("…")`` of a literal name; a name re-exported by an ``__init__.py`` counts as a use of
  the module that defines it; a relay module becomes a member. What isn't counted: an import under
  ``if TYPE_CHECKING`` (no runtime dependency), and a package's ``__init__.py`` with the modules
  below it (importing its own parts is what an ``__init__`` is for).
* ``private-import``: a ``_name`` another module defines, taken by ``from m import _name`` or used as
  ``m._name``. Dunders and private *modules* (``pkg._impl``) don't count. A re-export of one is
  attributed to the module that defines it.

Each cycle lists the edges inside it (``file:line``, names, whether every site is lazy) and
``cut``: the edge whose removal leaves the smallest circle, then the fewest names to move. It is a
starting point for a person, not a fix.

Not read: tests, code in other languages, dynamic imports of a computed name, ``from m import *``
names (the edge counts, the names don't). An import is resolved only to a module of the same repo;
a name two modules share resolves to the one nearest the importer, and to neither on a tie.

``run`` reads every mirror's ``origin/dev`` through git objects (never the working tree, never a
write) into ``reports/structure.json``. A repo that can't be read has an ``error`` and no counts, and
a file that doesn't parse is listed in ``unparsed``: neither may be read as clean (``complete``).
It opens no packs: that waits for the evidence check (#58).
"""
from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from auditor.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

REPORT_NAME = "structure.json"
MAX_FILE_BYTES = 1_000_000
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "fixtures", "testdata", "build",
                        "dist", "vendor", "third_party", "site-packages"})
_MAX_SITES = 5  # shown per edge


class Unreadable(Exception):
    """The repo's dev tree could not be read, so nothing is known about it."""


def in_scope(path: str) -> bool:
    from auditor.evidence import is_test_file

    p = PurePosixPath(path)
    return (p.suffix == ".py" and p.name != "conftest.py" and not is_test_file(path)
            and not _SKIP_DIRS.intersection(p.parts[:-1]))


# ── imports ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ref:
    """One imported name: ``from <module> import <name>`` (``name`` empty for ``import <module>``)."""
    module: str      # absolute dotted name, or relative to ``level``
    name: str
    level: int
    line: int
    lazy: bool       # inside a function
    bound: str = ""  # the local name it binds
    scope: int = 0   # 0 at module level, else an id of the enclosing function


@dataclass
class Source:
    refs: list[Ref] = field(default_factory=list)
    attrs: list[tuple[str, str, int, tuple[int, ...]]] = field(default_factory=list)  # dotted, _name, line, scopes
    top: dict[str, Ref] = field(default_factory=dict)  # names bound by a module-level import
    defs: set[str] = field(default_factory=set)        # names bound by a module-level def, class or assignment


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
        (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else ""


def _private(name: str) -> bool:
    return name.startswith("_") and name != "_" and not name.startswith("__")


def parse(text: str) -> Source:
    """The imports and private attribute uses in *text*. Raises ``SyntaxError``/``ValueError``."""
    with warnings.catch_warnings():  # an invalid escape in scanned code isn't our warning
        warnings.simplefilter("ignore")
        tree = ast.parse(text)
    src = Source()

    def walk(node: ast.AST, scopes: tuple[int, ...]) -> None:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                walk(child, scopes)
            return
        here = scopes[-1] if scopes else 0
        refs: list[Ref] = []
        if isinstance(node, ast.Import):
            refs = [Ref(a.name, "", 0, node.lineno, bool(scopes), a.asname or a.name, here) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            refs = [Ref(node.module or "", a.name, node.level, node.lineno, bool(scopes), a.asname or a.name, here)
                    for a in node.names]
        elif isinstance(node, ast.Call) and _dotted(node.func).rsplit(".", 1)[-1] in ("import_module", "__import__"):
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and not arg.value.startswith("."):
                refs = [Ref(arg.value, "", 0, node.lineno, bool(scopes), "", here)]
        elif isinstance(node, ast.Attribute) and _private(node.attr):
            base = _dotted(node.value)
            if base:
                src.attrs.append((base, node.attr, node.lineno, scopes))
        src.refs += refs
        if not scopes:
            src.top.update({r.bound: r for r in refs if r.bound})
        inner = scopes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inner = (*scopes, id(node))
        for child in ast.iter_child_nodes(node):
            walk(child, inner)

    for stmt in tree.body:
        walk(stmt, ())
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            src.defs.add(stmt.name)
        for n in getattr(stmt, "targets", [getattr(stmt, "target", None)]):
            src.defs |= {x.id for x in ast.walk(n) if isinstance(x, ast.Name)} if n is not None else set()
    return src


# ── resolving names to files ─────────────────────────────────────────────────

class Modules:
    """Which file of the repo a dotted name means, seen from a file of the repo."""

    def __init__(self, paths: set[str]):
        self.paths = paths
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.package: dict[str, tuple[str, ...]] = {}  # path -> its own dotted name, for relative imports
        for path in paths:
            parts = PurePosixPath(path).with_suffix("").parts
            names = parts[:-1] if parts[-1] == "__init__" else parts
            # from the repo top (namespace packages), below src/, and from the top of its package
            starts = {0} | {i + 1 for i, p in enumerate(parts[:-1]) if p == "src"}
            top = len(parts) - 1
            while top > 0 and "/".join([*parts[:top], "__init__.py"]) in paths:
                top -= 1
            starts.add(top)
            for s in starts:
                # a bare name below the repo top is a script's neighbour, not an importable package
                if len(names) - s == 1 and s > 0 and parts[-1] != "__init__" and parts[s - 1] != "src":
                    continue
                if names[s:]:
                    self.by_name[".".join(names[s:])].add(path)
            self.package[path] = names

    def find(self, dotted: str, importer: str) -> str | None:
        hits = self.by_name.get(dotted) or set()
        if "." not in dotted and not hits:
            near = str(PurePosixPath(importer).with_name(f"{dotted}.py"))
            # sibling scripts only: inside a package `import json` is the standard library
            if near in self.paths and str(PurePosixPath(importer).with_name("__init__.py")) not in self.paths:
                return near
        if len(hits) <= 1:
            return next(iter(hits), None)
        mine = PurePosixPath(importer).parts

        def shared(p: str) -> int:
            n = 0
            for a, b in zip(PurePosixPath(p).parts, mine, strict=False):
                if a != b:
                    break
                n += 1
            return n
        best = max(shared(p) for p in hits)
        nearest = [p for p in hits if shared(p) == best]
        return nearest[0] if len(nearest) == 1 else None

    def at(self, parts: tuple[str, ...]) -> str | None:
        base = "/".join(parts)
        return next((p for p in (f"{base}.py", f"{base}/__init__.py") if p in self.paths), None)

    def targets(self, ref: Ref, importer: str) -> tuple[str | None, str | None]:
        """``(module file, submodule file)``: the second when the imported name is itself a module."""
        if ref.level:
            pkg = self.package[importer]
            if PurePosixPath(importer).name != "__init__.py":
                pkg = pkg[:-1]
            up = ref.level - 1
            if up > len(pkg):
                return None, None
            base = pkg[:len(pkg) - up] + tuple(x for x in ref.module.split(".") if x)
            return self.at(base) if base else None, self.at((*base, ref.name)) if ref.name else None
        mod = self.find(ref.module, importer)
        return mod, self.find(f"{ref.module}.{ref.name}", importer) if ref.name else None


def _parent_and_child(a: str, b: str) -> bool:
    for x, y in ((a, b), (b, a)):
        p = PurePosixPath(x)
        if p.name == "__init__.py" and p.parent in PurePosixPath(y).parents:
            return True
    return False


# ── the scan ─────────────────────────────────────────────────────────────────

def _components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components of two or more nodes (Tarjan, iterative), sorted."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    for root in sorted(graph):
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on.add(root)
        work = [(root, iter(sorted(graph.get(root, ()))))]
        while work:
            node, it = work[-1]
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = len(index)
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(graph.get(nxt, ())))))
                    break
                if nxt in on:
                    low[node] = min(low[node], index[nxt])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp: list[str] = []
                    while True:
                        n = stack.pop()
                        on.discard(n)
                        comp.append(n)
                        if n == node:
                            break
                    if len(comp) > 1:
                        out.append(sorted(comp))
    return sorted(out)


def scan_sources(sources: dict[str, str]) -> dict[str, Any]:
    """Cycles and private imports in *sources* (repo-relative path -> text). Out-of-scope paths are ignored."""
    parsed: dict[str, Source] = {}
    unparsed: list[str] = []
    for path in sorted(p for p in sources if in_scope(p)):
        try:
            parsed[path] = parse(sources[path])
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            unparsed.append(path)
    mods = Modules({p for p in sources if in_scope(p)})

    def origin(path: str, name: str) -> tuple[str, str, bool]:
        """The module and name behind *name* of *path*, through ``__init__`` re-exports. The flag:
        the name turned out to be a module."""
        seen: set[tuple[str, str]] = set()
        while (path, name) not in seen:
            seen.add((path, name))
            src = parsed.get(path)
            if src is None or PurePosixPath(path).name != "__init__.py" or name in src.defs:
                break
            ref = src.top.get(name)
            if ref is None or not ref.name:
                break
            mod, sub = mods.targets(ref, path)
            if mod is not None and _binds(parsed.get(mod), ref.name):
                path, name = mod, ref.name
            elif sub is not None:
                return sub, "", True
            elif mod is not None:
                path, name = mod, ref.name
            else:
                break
        return path, name, False

    def resolve(ref: Ref, importer: str) -> tuple[str | None, str]:
        """``(file used, name used in it)``; the name is empty when a whole module is used."""
        mod, sub = mods.targets(ref, importer)
        # a name the package binds itself wins over a submodule of the same name, as at runtime
        if sub is not None and not (mod is not None and _binds(parsed.get(mod), ref.name, skip_module=sub, mods=mods,
                                                              path=mod)):
            return sub, ""
        if mod is None:
            return None, ""
        if not ref.name or ref.name == "*":
            return mod, ref.name
        path, name, is_mod = origin(mod, ref.name)
        return path, "" if is_mod else name

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    private: dict[str, dict[str, Any]] = {}

    def add_private(user: str, owner: str, name: str, line: int) -> None:
        key = f"private-import:{user}->{owner}::{name}"
        entry = private.setdefault(key, {"key": key, "path": user, "defined_in": owner, "name": name, "lines": []})
        if line not in entry["lines"]:
            entry["lines"].append(line)

    for path, src in parsed.items():
        bound: dict[tuple[int, str], str] = {}  # (scope, local dotted name) -> module file
        for ref in src.refs:
            target, name = resolve(ref, path)
            if target is None:
                continue
            if not name and ref.bound:
                bound[(ref.scope, ref.bound)] = target
            if target == path:
                continue
            if _private(name):
                add_private(path, target, name, ref.line)
            if _parent_and_child(path, target):
                continue
            e = edges.setdefault((path, target), {"names": set(), "sites": [], "lazy": True})
            e["names"].add(name or "(module)")
            e["sites"].append(ref.line)
            e["lazy"] = e["lazy"] and ref.lazy
        for base, name, line, scopes in src.attrs:
            target = next((bound[(s, base)] for s in (*reversed(scopes), 0) if (s, base) in bound), None)
            if target is not None and target != path and not _has_child(mods, target, name):
                add_private(path, *origin(target, name)[:2], line)

    graph: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        graph[a].add(b)
    cycles = [_cycle(members, edges) for members in _components(graph)]
    for p in private.values():
        p["lines"].sort()
    return {"modules": len(parsed) + len(unparsed), "unparsed": unparsed, "complete": not unparsed,
            "cycles": cycles, "private_imports": [private[k] for k in sorted(private)]}


def _binds(src: Source | None, name: str, *, skip_module: str = "", mods: Modules | None = None, path: str = "") -> bool:
    """Whether a module binds *name* itself; with *skip_module*, not counting an import of that module."""
    if src is None:
        return False
    if name in src.defs:
        return True
    ref = src.top.get(name)
    if ref is None:
        return False
    return not (skip_module and mods is not None and mods.targets(ref, path)[1] == skip_module)


def _has_child(mods: Modules, path: str, name: str) -> bool:
    p = PurePosixPath(path)
    return p.name == "__init__.py" and mods.at((*p.parent.parts, name)) is not None


def _cycle(members: list[str], edges: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    inside = {k: v for k, v in sorted(edges.items()) if k[0] in members and k[1] in members}

    def left(cut: tuple[str, str]) -> int:
        g: dict[str, set[str]] = defaultdict(set)
        for a, b in inside:
            if (a, b) != cut:
                g[a].add(b)
        return max((len(c) for c in _components(g)), default=0)
    best = min(inside, key=lambda k: (left(k), len(inside[k]["names"]), len(inside[k]["sites"]), k))
    return {
        "key": "module-cycle:" + ",".join(members), "members": members, "size": len(members),
        "lazy_edges": sum(v["lazy"] for v in inside.values()),
        "edges": [{"from": a, "to": b, "names": sorted(v["names"]), "lazy": v["lazy"],
                   "sites": [f"{a}:{n}" for n in sorted(set(v["sites"]))[:_MAX_SITES]]}
                  for (a, b), v in inside.items()],
        "cut": {"from": best[0], "to": best[1], "names": sorted(inside[best]["names"]), "largest_left": left(best)},
    }


# ── mirrors ──────────────────────────────────────────────────────────────────

def read_dev(mirror_path: Path | None) -> tuple[str, dict[str, str], list[str]]:
    """``(dev sha, in-scope sources, files that couldn't be read)`` from a mirror's git objects."""
    from auditor.pack_gc import Mirror

    m = Mirror(mirror_path)
    if not m.ok or m.path is None:
        raise Unreadable("no mirror for this repo")
    if m.ref != "origin/dev":  # HEAD is whatever a checkout happens to be on
        raise Unreadable("the mirror has no origin/dev")

    def git(*args: str, data: bytes | None = None) -> bytes:
        r = subprocess.run(["git", *args], cwd=m.path, capture_output=True, timeout=120, check=False, input=data)
        if r.returncode != 0:
            raise Unreadable(f"git {args[0]}: {r.stderr.decode(errors='replace')[:200]}")
        return r.stdout
    sha = git("rev-parse", m.ref).decode().strip()
    blobs: list[tuple[str, str]] = []
    skipped: list[str] = []
    for row in git("ls-tree", "-r", "-l", "-z", sha).split(b"\0"):
        if not row:
            continue
        meta, _, raw = row.partition(b"\t")
        mode, kind, oid, size = meta.split()
        path = raw.decode("utf-8", errors="surrogateescape")
        if kind != b"blob" or mode == b"120000" or not in_scope(path):
            continue
        if int(size) > MAX_FILE_BYTES:
            skipped.append(path)
        else:
            blobs.append((path, oid.decode()))
    out = git("cat-file", "--batch", data="".join(f"{o}\n" for _, o in blobs).encode()) if blobs else b""
    sources: dict[str, str] = {}
    at = 0
    for path, oid in blobs:
        end = out.index(b"\n", at)
        head = out[at:end].split()
        if len(head) != 3 or head[0].decode() != oid:
            raise Unreadable(f"git object missing for {path}")
        n = int(head[2])
        sources[path] = out[end + 1:end + 1 + n].decode("utf-8-sig", errors="replace")
        at = end + 1 + n + 1
    return sha, sources, skipped


def scan_mirror(path: Path | None) -> dict[str, Any]:
    try:
        sha, sources, skipped = read_dev(path)
    except (Unreadable, OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"error": str(exc)[:300], "complete": False}
    r = scan_sources(sources)
    r["unparsed"] = sorted([*r["unparsed"], *skipped])
    r["complete"] = not r["unparsed"]
    return {"dev_sha": sha, **r}


def run(output_dir: str | Path | None = None, *, mirrors_path: str | Path = "config/repo_mirrors.yaml",
        repos: list[str] | None = None) -> dict[str, Any]:
    """Scan every mirror's dev into ``structure.json``. With *repos*, the others keep their last result."""
    from auditor.pack_gc import mirror_paths

    out = Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")
    out.mkdir(parents=True, exist_ok=True)
    paths = mirror_paths(mirrors_path)
    if unknown := sorted(set(repos or ()) - set(paths)):
        raise ValueError(f"no mirror configured for: {', '.join(unknown)}")
    t0 = time.monotonic()
    prev = ((load_report(out) or {}).get("repos") or {}) if repos else {}
    results = {name: scan_mirror(p) if not repos or name in repos else prev[name]
               for name, p in sorted(paths.items()) if not repos or name in repos or name in prev}
    report = {"at": datetime.now(UTC).isoformat(), "seconds": round(time.monotonic() - t0, 1),
              "summary": summarize(results), "repos": results}
    atomic_write_text(out / REPORT_NAME, json.dumps(report, indent=1))
    return report


def summarize(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results.values() if not r.get("error")]
    return {"repos": len(results), "unreadable": len(results) - len(ok),
            "incomplete": sum(not r["complete"] for r in ok),
            "cycles": sum(len(r["cycles"]) for r in ok),
            "repos_with_cycles": sum(bool(r["cycles"]) for r in ok),
            "private_imports": sum(len(r["private_imports"]) for r in ok)}


def load_report(output_dir: str | Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(output_dir) / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_quietly(output_dir: str | Path | None = None, **kw: Any) -> dict[str, Any]:
    """Pipeline tail: never fail the caller."""
    try:
        return run(output_dir, **kw)["summary"]
    except Exception as exc:  # noqa: BLE001 — reported, not fatal
        logger.warning("structure scan failed: %s", exc)
        return {"error": str(exc)[:300]}
