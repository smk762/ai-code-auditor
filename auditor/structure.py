"""Structure inside a repo (#154): module cycles and private imports. Report-only, no LLM.

``arch_contracts`` covers repo-to-repo imports; nothing looked inside a repo. Two checks, Python only:

* ``module-cycle``: modules that use each other in a circle, one entry per cycle (a strongly connected
  component), keyed by its sorted member paths, never by line. It can't be hidden cosmetically:
  an import inside a function counts (it is marked ``lazy``), and so do ``import_module("…")`` and
  ``__import__("…")`` of a literal name; a name re-exported by an ``__init__.py`` counts as a use of
  the module that defines it; a relay module becomes a member. What isn't counted: an import under
  ``if TYPE_CHECKING`` (no runtime dependency), and a module with the ``__init__.py`` of a package it
  is inside. Importing from another package does count its ``__init__.py`` and that of every package
  above it, because Python runs them first: such an edge is ``implicit`` unless the file also imports
  it by name, and its names say which import runs it (``via <module>``).
* ``private-import``: a ``_name`` another module defines, taken by ``from m import _name`` or used as
  ``m._name`` or ``getattr(m, "_name")``. Dunders and private *modules* (``pkg._impl``) don't count. A re-export of one is
  attributed to the module that defines it.

Each cycle lists the edges inside it (``file:line``, names, whether every site is lazy) and
``cuts``: the fewest edges whose removal leaves no circle, then the fewest names to move. It is a hint,
not a fix: it counts edges and can't see which dependency is the real one. The names on a cut usually
belong in a module both ends may use, and a lazy import or a relay removes nothing.

Not read: tests, code in other languages, dynamic imports of a computed name, ``from m import *``
names (the edge counts, the names don't). An import is resolved only to a module of the same repo;
a name two modules share resolves to the one nearest the importer, and to neither on a tie.

``run`` reads every mirror's ``origin/dev`` through git objects (never the working tree, never a
write) into ``reports/structure.json``. A repo that can't be read has an ``error`` and no counts, and
a file that doesn't parse is listed in ``unparsed``: neither may be read as clean (``complete``).
Parsing happens in a child on ``AI_AUDIT_SCAN_PYTHON`` (the image bakes in a newer one than its own,
because a file using newer syntax than the parser's is unreadable); this module stays stdlib-only so
that interpreter needs no packages. Unset means this interpreter; set but unusable is an ``error``.
It opens no packs itself (``structure_packs``: a person approves). ``judge`` is the evidence check for structural packs (#58): ``validate_patch`` scans
dev and dev plus the patch, and the pack resolves only when the violation is really gone.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
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
_MAX_CUT_STEPS = 3000  # cut sets tried per cycle


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
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
              and isinstance(node.args[1].value, str) and _private(node.args[1].value)):
            base = _dotted(node.args[0])
            if base:
                src.attrs.append((base, node.args[1].value, node.lineno, scopes))
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


def _own_package(a: str, b: str) -> bool:
    """*b* is the ``__init__.py`` of a package *a* is inside."""
    p = PurePosixPath(b)
    return p.name == "__init__.py" and p.parent in PurePosixPath(a).parents


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

    def add_edge(a: str, b: str, name: str, ref: Ref, *, implicit: bool) -> None:
        if a == b or _own_package(a, b):
            return
        e = edges.setdefault((a, b), {"names": set(), "via": set(), "sites": [], "lazy": True, "implicit": True})
        e["via" if implicit else "names"].add(name)
        e["sites"].append(ref.line)
        e["lazy"] = e["lazy"] and ref.lazy
        e["implicit"] = e["implicit"] and implicit

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
            loaded = {target, mods.targets(ref, path)[0] or target}  # the package named may re-export the target
            for init in {f"{d.as_posix()}/__init__.py" for m in loaded for d in PurePosixPath(m).parents} & set(parsed):
                if init != target:
                    add_edge(path, init, f"via {target}", ref, implicit=True)
            if not name and ref.bound:
                bound[(ref.scope, ref.bound)] = target
            if target == path:
                continue
            if _private(name):
                add_private(path, target, name, ref.line)
            add_edge(path, target, name or "(module)", ref, implicit=False)
        for base, name, line, scopes in src.attrs:
            target = next((bound[(s, base)] for s in (*reversed(scopes), 0) if (s, base) in bound), None)
            if target is None or target == path or _has_child(mods, target, name):
                continue
            owner, used, is_mod = origin(target, name)  # as the ref loop: a module or a public name is no finding
            if not is_mod and _private(used):
                add_private(path, owner, used, line)

    graph: dict[str, set[str]] = defaultdict(set)
    for (a, b), e in edges.items():
        graph[a].add(b)
        e["names"] = e["names"] or e["via"]
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


def _loop(graph: dict[str, set[str]]) -> list[tuple[str, str]]:
    """The edges of a shortest circle in *graph*, or [] when it has none."""
    best: list[tuple[str, str]] = []
    for start in sorted(graph):
        prev: dict[str, str] = {}
        queue, seen = [start], {start}
        for node in queue:
            for nxt in sorted(graph.get(node, ())):
                if nxt == start:
                    path = [(node, start)]
                    while node != start:
                        path.append((prev[node], node))
                        node = prev[node]
                    if not best or len(path) < len(best):
                        best = path[::-1]
                    queue.clear()
                    break
                if nxt not in seen:
                    seen.add(nxt)
                    prev[nxt] = node
                    queue.append(nxt)
            else:
                continue
            break
    return best


def breaks_every_circle(edges: set[tuple[str, str]]) -> bool:
    """No circle is left among these ``(importer, imported)`` pairs."""
    graph: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        graph[a].add(b)
    return not _loop(graph)


def _cuts(inside: dict[tuple[str, str], dict[str, Any]]) -> tuple[list[tuple[str, str]], bool]:
    """The fewest edges whose removal leaves no circle, then the fewest names to move, then the most
    lazy ones. The flag is False when the search was cut short (``_MAX_CUT_STEPS``) and the set may
    not be the smallest; it still breaks every circle."""
    def cost(cut: frozenset) -> tuple:
        return (len(cut), sum(len(inside[e]["names"]) for e in cut), sum(not inside[e]["lazy"] for e in cut), sorted(cut))

    def loop(cut: frozenset) -> list[tuple[str, str]]:
        g: dict[str, set[str]] = defaultdict(set)
        for a, b in inside:
            if (a, b) not in cut:
                g[a].add(b)
        return _loop(g)

    best: frozenset = frozenset()
    while circle := loop(best):  # a first answer, so a search cut short still has one
        best |= {min(circle, key=lambda e: (len(inside[e]["names"]), not inside[e]["lazy"], e))}
    for e in sorted(best):  # drop what later cuts made needless
        if not loop(best - {e}):
            best -= {e}
    steps, seen, stack = 0, set(), [frozenset()]
    while stack:
        cut = stack.pop()
        if cut in seen or len(cut) > len(best):
            continue
        seen.add(cut)
        steps += 1
        if steps > _MAX_CUT_STEPS:
            return sorted(best), False
        circle = loop(cut)
        if not circle:
            best = min(best, cut, key=cost)
        elif len(cut) < len(best):  # every answer cuts some edge of this circle
            stack.extend(cut | {e} for e in circle)
    return sorted(best), True


def _cycle(members: list[str], edges: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    inside = {k: v for k, v in sorted(edges.items()) if k[0] in members and k[1] in members}

    cuts, smallest = _cuts(inside)
    return {
        "key": "module-cycle:" + ",".join(members), "members": members, "size": len(members),
        "lazy_edges": sum(v["lazy"] for v in inside.values()),
        "edges": [{"from": a, "to": b, "names": sorted(v["names"]), "lazy": v["lazy"],
                   "implicit": v["implicit"],
                   "sites": [f"{a}:{n}" for n in sorted(set(v["sites"]))[:_MAX_SITES]]}
                  for (a, b), v in inside.items()],
        "cuts": [{"from": a, "to": b, "names": sorted(inside[a, b]["names"])} for a, b in cuts],
        "cuts_smallest": smallest,
    }


# ── did a patch fix it? (#58) ────────────────────────────────────────────────

def judge(pack: dict[str, Any], base: dict[str, Any], patched: dict[str, Any]) -> dict[str, Any]:
    """``{"gone", "reason"}``: whether *patched* no longer has the violation a structural pack is for.

    The pack's ``governance.structure`` is a scan entry: a cycle's ``members``, or a private import's
    ``path``, ``defined_in`` and ``name``. Keys aren't compared, because a relay module or a rename
    changes a key and keeps the problem. Gone means: dev shows it, the patched tree doesn't, and the
    patched tree has no cycle (or private import) that dev lacks, which is where a moved one shows up.
    Anything unread on the patched tree means unknown, and unknown is not gone."""
    def no(reason: str) -> dict[str, Any]:
        return {"gone": False, "reason": reason}

    for name, scan in (("dev", base), ("patched", patched)):
        if scan.get("error"):
            return no(f"the structure scan couldn't read the {name} tree: {scan['error']}")
    if patched.get("unparsed") or not patched.get("complete"):
        return no(f"the patched tree has files the scan can't read: {', '.join(patched.get('unparsed') or [])[:300]}")
    want = (pack.get("governance") or {}).get("structure") or {}
    if pack.get("kind") == "module_cycle":
        members = {str(m) for m in want.get("members") or ()}
        if len(members) < 2:
            return no("the pack names no cycle members")
        was = [set(c["members"]) for c in base["cycles"]]
        now = [set(c["members"]) for c in patched["cycles"]]
        if not any(len(c & members) > 1 for c in was):
            return no("dev doesn't have this cycle, so the patch can't be what fixed it")
        left = next((c for c in now if c & members), None)
        if left:
            return no(f"still in a cycle: {', '.join(sorted(left))[:400]}")
        new = next((c for c in now if not any(c <= w for w in was)), None)
        if new:
            return no(f"the patch adds or moves a cycle: {', '.join(sorted(new))[:400]}")
        return {"gone": True, "reason": "the cycle is on dev and gone with the patch, and none was added"}
    if pack.get("kind") == "private_import":
        path, target = str(want.get("path") or ""), (want.get("defined_in"), want.get("name"))
        if not path or not all(target):
            return no("the pack names no private import")
        hit = [p for p in base["private_imports"] if p["path"] == path and (p["defined_in"], p["name"]) == target]
        if not hit:
            return no("dev doesn't have this private import, so the patch can't be what fixed it")
        keys = {p["key"] for p in patched["private_imports"]}
        if hit[0]["key"] in keys:
            return no(f"still there: {hit[0]['key']}")
        new_keys = sorted(keys - {p["key"] for p in base["private_imports"]})
        if new_keys:
            return no(f"the patch adds or moves a private import: {', '.join(new_keys[:3])}")
        cycles = [c for c in patched["cycles"] if not any(set(c["members"]) <= set(w["members"]) for w in base["cycles"])]
        if cycles:
            return no(f"the patch adds a cycle: {', '.join(cycles[0]['members'])[:400]}")
        return {"gone": True, "reason": "the private import is on dev and gone with the patch, and none was added"}
    return no(f"not a structural pack: {pack.get('kind')}")


def scan_tree(root: Path) -> dict[str, Any]:
    """Scan a directory's files. Symlinks are never followed."""
    sources: dict[str, str] = {}
    skipped: list[str] = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
        for f in files:
            p = Path(d) / f
            rel = p.relative_to(root).as_posix()
            if p.is_symlink() or not in_scope(rel):
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    skipped.append(rel)
                else:
                    sources[rel] = p.read_bytes().decode("utf-8-sig", errors="replace")
            except OSError:
                skipped.append(rel)
    return _with_skipped(scan_sources(sources), skipped)


def _with_skipped(r: dict[str, Any], skipped: list[str]) -> dict[str, Any]:
    unparsed = sorted([*r["unparsed"], *skipped])
    return {**r, "unparsed": unparsed, "complete": not unparsed}


def scan_python() -> str:
    return os.getenv("AI_AUDIT_SCAN_PYTHON", "").strip() or sys.executable


def scan_tree_isolated(root: Path, *, timeout_s: int = 120) -> dict[str, Any]:
    """``scan_tree`` in a child process: it is CPU-bound, and it parses untrusted source, which can
    crash an interpreter. Any failure is an ``error``, never an empty scan."""
    return _in_child(str(root.resolve()), None, timeout_s)


def scan_sources_isolated(sources: dict[str, str], *, timeout_s: int = 300) -> dict[str, Any]:
    """``scan_sources`` in the same kind of child, with the same failures."""
    return _in_child("-", json.dumps(sources), timeout_s)


def _in_child(arg: str, stdin: str | None, timeout_s: int) -> dict[str, Any]:
    from auditor.proc import child_env
    from auditor.proc import run as proc_run

    try:
        proc = proc_run([scan_python(), "-m", "auditor.structure", arg],
                        cwd=Path(__file__).resolve().parents[1], env=child_env(), capture_output=True, text=True,
                        timeout=timeout_s, input=stdin)
        if proc.returncode != 0:
            return {"error": f"scan exited {proc.returncode}: {(proc.stderr or '')[-200:]}", "complete": False}
        data = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)[:300], "complete": False}
    return data if isinstance(data, dict) else {"error": "the scan returned no report", "complete": False}


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
        try:
            r = subprocess.run(["git", *args], cwd=m.path, capture_output=True, timeout=120, check=False, input=data)
        except (OSError, subprocess.SubprocessError) as exc:  # a timeout, or no git
            raise Unreadable(f"git {args[0]}: {exc}"[:300]) from exc
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
        end = out.find(b"\n", at)
        head = out[at:end].split() if end != -1 else []
        if len(head) != 3 or head[0].decode() != oid or not head[2].isdigit():
            raise Unreadable(f"git object missing for {path}")
        n = int(head[2])
        if end + 1 + n > len(out):  # cut short: a partial file must not be read as the whole one
            raise Unreadable(f"git output cut short at {path}")
        sources[path] = out[end + 1:end + 1 + n].decode("utf-8-sig", errors="replace")
        at = end + 1 + n + 1
    return sha, sources, skipped


def scan_mirror(path: Path | None) -> dict[str, Any]:
    try:
        sha, sources, skipped = read_dev(path)
    except (Unreadable, OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"error": str(exc)[:300], "complete": False}
    scan = scan_sources_isolated(sources)
    return scan if scan.get("error") else {"dev_sha": sha, **_with_skipped(scan, skipped)}


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


if __name__ == "__main__":  # _in_child's child
    if sys.argv[1] == "-":
        print(json.dumps(scan_sources(json.loads(sys.stdin.read()))))
    else:
        print(json.dumps(scan_tree(Path(sys.argv[1]))))
