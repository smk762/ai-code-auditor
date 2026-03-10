from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from auditor.contracts import CodeUnit


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_code_units(repo_name: str, file_path: Path) -> list[CodeUnit]:
    language = file_path.suffix.lstrip(".")
    text = file_path.read_text(encoding="utf-8", errors="ignore")

    if file_path.suffix == ".py":
        return _extract_python_units(repo_name, file_path, text)
    return [_whole_file_unit(repo_name, file_path, text, language)]


def _whole_file_unit(repo_name: str, file_path: Path, text: str, language: str) -> CodeUnit:
    line_count = text.count("\n") + 1
    unit_id = _hash_text(f"{repo_name}:{file_path}:module")
    return CodeUnit(
        id=unit_id,
        repo=repo_name,
        file_path=str(file_path),
        symbol=file_path.stem,
        kind="module",
        language=language,
        start_line=1,
        end_line=line_count,
        ast_features={"imports": []},
        raw_text=text,
        raw_text_hash=_hash_text(text),
    )


def _extract_python_units(repo_name: str, file_path: Path, text: str) -> list[CodeUnit]:
    line_count = text.count("\n") + 1
    units: list[CodeUnit] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [_whole_file_unit(repo_name, file_path, text, "python")]

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", line_count)
            symbol = node.name
            snippet = "\n".join(text.splitlines()[start - 1 : end])
            unit_id = _hash_text(f"{repo_name}:{file_path}:{symbol}:{start}:{end}")
            units.append(
                CodeUnit(
                    id=unit_id,
                    repo=repo_name,
                    file_path=str(file_path),
                    symbol=symbol,
                    kind="class" if isinstance(node, ast.ClassDef) else "function",
                    language="python",
                    start_line=start,
                    end_line=end,
                    ast_features={"imports": imports},
                    raw_text=snippet,
                    raw_text_hash=_hash_text(snippet),
                )
            )

    if not units:
        units.append(_whole_file_unit(repo_name, file_path, text, "python"))
    return units
