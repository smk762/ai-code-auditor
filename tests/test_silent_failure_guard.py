"""Silent-failure class guard (``pack-class-silent-failure``), the same rule agent-composer enforces.

CI runs ruff ``B905,S110,S112`` over the repo (``.github/workflows/lint.yml``). This test is the same
rule in stdlib, plus one thing ruff doesn't check: a ``noqa`` for it must give a reason.

* ``zip()`` with arguments says ``strict=``: ``True`` when the lengths must match (a mismatch
  raises), ``False`` when shorter is intended.
* An ``except`` (bare, ``Exception`` or ``BaseException``) whose whole body is ``pass`` or
  ``continue`` swallows the error. Log it, narrow it, or mark the ``except`` line
  ``# noqa: S110 <why nothing is lost>`` (``S112`` for ``continue``).
* So does one whose whole body returns an empty value (``return {}``, ``[]``, ``""``, ``0``,
  ``False``, ``None``, ``set()``…, or a bare ``return``): a corrupt state file read as empty state
  looks like success. No ruff rule covers it, so only this test does; it reports ``S110`` so one
  reasoned ``noqa`` works for both. The seeded-recall gate (#63) found this gap.
* The same swallow rewritten (ai-code-auditor#155) is the same failure: a body of only ``...``, a
  constant or ``_ = exc``; one that only assigns an empty value (``state = {}``) for the code after it;
  and ``contextlib.suppress(Exception)``. Ruff sees none of them, so they are ``S110`` here.

It fires on ``fixtures/silent_failure_guard/unguarded.py``, stays quiet on ``guarded.py``, and
finds nothing in the repo's own code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "silent_failure_guard"
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "fixtures", "build", "dist",
             ".claude", "reports", "alembic"}
MIN_REASON = 10
_BROAD = {"Exception", "BaseException"}


def _reasoned_noqa(line: str, code: str) -> bool:
    """A ``# noqa:`` naming *code* whose text after the whole code list is a real reason."""
    m = re.search(r"#\s*noqa:\s*((?:[A-Z]+[0-9]+[,\s]*)+)(.*)$", line)
    if not m or code not in re.findall(r"[A-Z]+[0-9]+", m.group(1)):
        return False
    return len(m.group(2).strip(" —-:")) >= MIN_REASON


def _broad(handler: ast.ExceptHandler) -> bool:
    """Bare ``except``, a broad name, or a tuple containing one."""
    t = handler.type
    if t is None:
        return True
    elts = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(isinstance(e, ast.Name) and e.id in _BROAD for e in elts)


_EMPTY_CALLS = frozenset({"dict", "list", "set", "tuple", "str", "bytes", "frozenset"})


def _empty(value: ast.expr | None) -> bool:
    """A return value that reads as "nothing here": None, a falsy constant, an empty literal or call."""
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        return value.value in (None, False, 0, "", b"") and not isinstance(value.value, float)
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return not value.elts
    if isinstance(value, ast.Dict):
        return not value.keys
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _EMPTY_CALLS
            and not value.args and not value.keywords)


def _inert(stmt: ast.stmt) -> bool:
    """A statement that does nothing: ``pass``, ``...``, a bare constant, or a name or constant put in ``_``."""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr):
        return isinstance(stmt.value, ast.Constant)
    return (isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.Name, ast.Constant))
            and all(isinstance(t, ast.Name) and t.id == "_" for t in stmt.targets))


def _empty_reset(stmt: ast.stmt) -> bool:
    """An assignment of an empty value: ``state = {}``, for the code after the ``try`` to use."""
    return isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None and _empty(stmt.value)


def _swallows(body: list[ast.stmt]) -> str:
    """The code for a broad handler's body that loses the error, else ``""``."""
    rest = [s for s in body if not _inert(s)]
    if not rest:
        return "S110"
    if len(rest) > 1:
        return ""
    if isinstance(rest[0], ast.Continue):
        return "S112"
    empty_return = isinstance(rest[0], ast.Return) and _empty(rest[0].value)
    return "S110" if empty_return or _empty_reset(rest[0]) else ""


def _broad_suppress(call: ast.Call) -> bool:
    """``suppress(Exception)``: an except-pass spelled as a context manager."""
    name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
    return name == "suppress" and any(isinstance(a, ast.Name) and a.id in _BROAD for a in call.args)


def findings(path: Path) -> list[str]:
    """``file:line code`` for each silent-failure pattern in *path* without a reasoned noqa."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    out: list[str] = []

    def hit(lineno: int, code: str) -> None:
        if not _reasoned_noqa(lines[lineno - 1], code):
            out.append(f"{path.relative_to(REPO)}:{lineno} {code}")

    for node in ast.walk(ast.parse(source, filename=str(path))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "zip"
                and node.args and not any(k.arg == "strict" for k in node.keywords)):
            hit(node.lineno, "B905")
        elif isinstance(node, ast.Call) and _broad_suppress(node):
            hit(node.lineno, "S110")
        elif isinstance(node, ast.ExceptHandler) and _broad(node):
            if code := _swallows(node.body):
                hit(node.lineno, code)
    return out


def repo_files() -> list[Path]:
    return sorted(p for p in REPO.rglob("*.py")
                  if not SKIP_DIRS.intersection(p.relative_to(REPO).parts[:-1]))


def test_fires_on_the_positive_fixture() -> None:
    hits = findings(FIXTURES / "unguarded.py")
    # zip, except-continue, bare except-pass, a noqa that gives no reason, and two empty returns, then the rewrites
    assert sorted(h.rsplit(" ", 1)[1] for h in hits) == ["B905", *["S110"] * 9, "S112"], hits


def test_silent_on_the_negative_fixture() -> None:
    assert findings(FIXTURES / "guarded.py") == []


@pytest.mark.parametrize(("line", "code", "ok"), [
    ("except Exception:  # noqa: BLE001, S110, E722", "S110", False),
    ("except Exception:  # noqa: S110, BLE001, E722", "S110", False),
    ("except Exception:  # noqa: S110 best-effort cleanup only", "S110", True),
    ("except Exception:  # noqa: BLE001, S110 — best-effort cleanup only", "S110", True),
    ("x  # noqa: S112 best-effort cleanup only", "S110", False),
])
def test_trailing_codes_are_not_a_reason(line: str, code: str, ok: bool) -> None:
    assert _reasoned_noqa(line, code) is ok


def test_tuple_with_a_broad_type_is_broad() -> None:
    for src in ("try:\n    f()\nexcept (Exception, OSError):\n    pass\n",
                "try:\n    f()\nexcept (Exception,):\n    pass\n"):
        assert _broad(ast.parse(src).body[0].handlers[0]), src
    assert not _broad(ast.parse("try:\n    f()\nexcept (OSError, ValueError):\n    pass\n").body[0].handlers[0])


def test_repo_has_no_silent_failure() -> None:
    files = repo_files()
    assert len(files) > 100, "the scan found too few files: REPO moved?"
    hits = [h for f in files for h in findings(f)]
    assert hits == [], "say zip(strict=...), log or narrow the except, or add '# noqa: S110 <reason>'"
