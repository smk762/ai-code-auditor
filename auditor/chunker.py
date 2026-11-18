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


def _is_docstring(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _extract_python_units(repo_name: str, file_path: Path, text: str) -> list[CodeUnit]:
    # Line numbers must agree with the AST, which breaks lines only on \n, \r\n and \r
    # (str.splitlines() also breaks on form feeds, \x1c-\x1e, \x85, \u2028, \u2029).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    source_lines = text.split("\n")
    line_count = len(source_lines)
    units: list[CodeUnit] = []
    try:
        tree = ast.parse(text, filename=str(file_path))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # Unparsable (NUL bytes, absurd nesting): audit the file whole instead of failing.
        return [_whole_file_unit(repo_name, file_path, text, "python")]

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    residual: list[ast.stmt] = []
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Decorators (routes, auth, @dataclass) belong to the unit; node.lineno is
            # the def/class line, after them.
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = getattr(node, "end_lineno", line_count)
            symbol = node.name
            snippet = "\n".join(source_lines[start - 1 : end])
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
        elif not isinstance(node, (ast.Import, ast.ImportFrom)) and not (i == 0 and _is_docstring(node)):
            residual.append(node)

    if not units:
        units.append(_whole_file_unit(repo_name, file_path, text, "python"))
    elif residual:
        # Module-level code outside any def/class (config, secrets, route registration,
        # import-time calls, __main__ blocks) is audited as one module unit.
        start = residual[0].lineno
        end = getattr(residual[-1], "end_lineno", None) or residual[-1].lineno
        snippet = "\n".join(
            "\n".join(source_lines[n.lineno - 1 : (getattr(n, "end_lineno", None) or n.lineno)])
            for n in residual
        )
        units.append(
            CodeUnit(
                id=_hash_text(f"{repo_name}:{file_path}:<module>:{start}:{end}"),
                repo=repo_name,
                file_path=str(file_path),
                symbol="<module>",
                kind="module",
                language="python",
                start_line=start,
                end_line=end,
                ast_features={"imports": imports},
                raw_text=snippet,
                raw_text_hash=_hash_text(snippet),
            )
        )
    return units
