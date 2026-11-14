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


def file_structure(path: Path, text: str) -> dict[str, Any]:
    """Imports, exports and top-level names, read from the code itself."""
    out: dict[str, Any] = {"imports": [], "exports": None, "top_level": []}
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        imps: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imps += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = "." * node.level + (node.module or "")
                imps.append(f"{mod}: {', '.join(a.name for a in node.names)}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out["top_level"].append(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        try:
                            out["exports"] = list(ast.literal_eval(node.value))
                        except (ValueError, SyntaxError):
                            out["exports"] = ["<dynamic __all__>"]
        # lazy imports inside functions matter for "optional dependency" claims
        top = set(map(id, tree.body))
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
