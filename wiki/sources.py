"""Source files of a repo and the structure facts read from them (no LLM)."""
from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any

from auditor.repo_resolver import resolve_scan_root
from auditor.repo_scanner import scan_repo

SKIP_DIRS = {"vendor", "vendored", "third_party"}
MAX_FILE_CHARS = 200_000  # larger files are skipped and reported, never truncated


def source_files(repo) -> list[Path]:
    root = resolve_scan_root(repo)
    return [f for f in sorted(scan_repo(repo)) if not SKIP_DIRS.intersection(f.relative_to(root).parts)]


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def numbered(text: str) -> str:
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), 1))


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


_JS_IMPORT = re.compile(r"""^\s*(?:import\b[^'"]*?from\s*|import\s*|export\b[^'"]*?from\s*)['"]([^'"]+)['"]""", re.M)
_JS_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_EXPORT = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|type|interface|enum)\s+(\w+)", re.M)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
        (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _module_imports(stmts: list[ast.stmt], type_only: bool = False):
    """(import node, under ``if TYPE_CHECKING``) for every import that runs at module level, including
    those in module-level ``if``/``try``/``with`` blocks, but not inside function or class bodies."""
    for node in stmts:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, type_only
        elif isinstance(node, ast.If):
            yield from _module_imports(node.body, type_only or _is_type_checking(node.test))
            yield from _module_imports(node.orelse, type_only)
        elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            yield from _module_imports(node.body, type_only)
            for h in node.handlers:
                yield from _module_imports(h.body, type_only)
            yield from _module_imports(node.orelse, type_only)
            yield from _module_imports(node.finalbody, type_only)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _module_imports(node.body, type_only)


def file_structure(path: Path, text: str) -> dict[str, Any]:
    """Imports, exports and top-level names, read from the code itself."""
    out: dict[str, Any] = {"imports": [], "exports": None, "top_level": []}
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        imps: list[str] = []
        module_level = list(_module_imports(tree.body))
        for node, type_only in module_level:
            tag = " (type checking only)" if type_only else ""
            if isinstance(node, ast.Import):
                imps += [a.name + tag for a in node.names]
            else:
                mod = "." * node.level + (node.module or "")
                imps.append(f"{mod}: {', '.join(a.name for a in node.names)}{tag}")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out["top_level"].append(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        try:
                            out["exports"] = list(ast.literal_eval(node.value))
                        except (ValueError, SyntaxError):
                            out["exports"] = ["<dynamic __all__>"]
        # lazy imports inside functions matter for "optional dependency" claims; module-level
        # ``try:``/``if`` blocks (optional or version-gated imports, TYPE_CHECKING) are not lazy
        top = {id(n) for n, _ in module_level}
        lazy = sorted({a.name for n in ast.walk(tree) if isinstance(n, ast.Import) and id(n) not in top for a in n.names}
                      | {n.module or "" for n in ast.walk(tree)
                         if isinstance(n, ast.ImportFrom) and id(n) not in top and n.level == 0})
        out["imports"] = imps
        if lazy:
            out["lazy_imports"] = lazy
    elif path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
        out["imports"] = sorted(set(_JS_IMPORT.findall(text)) | set(_JS_REQUIRE.findall(text)))
        out["exports"] = _JS_EXPORT.findall(text) or None
    return out


def structure_block(repo) -> str:
    """Markdown block of structure facts for every source file (authoritative for the model)."""
    root = resolve_scan_root(repo)
    lines = ["## Structure facts (read from the code, authoritative)\n",
             "Imports are module-level unless marked lazy (imported inside a function). "
             "`exports` is `__all__` (Python) or `export` declarations (JS/TS); "
             "null means the file declares none.\n"]
    for f in source_files(repo):
        text = read_text(f)
        if text is None or len(text) > MAX_FILE_CHARS:
            continue
        s = file_structure(f, text)
        if not (s["imports"] or s["exports"] or s["top_level"]):
            continue
        lines.append(f"\n### {rel(f, root)}")
        if s["imports"]:
            lines.append("- imports: " + "; ".join(s["imports"]))
        if s.get("lazy_imports"):
            lines.append("- lazy imports: " + ", ".join(s["lazy_imports"]))
        lines.append(f"- exports: {s['exports']}")
        if s["top_level"]:
            lines.append("- top-level defs: " + ", ".join(s["top_level"]))
    return "\n".join(lines)
