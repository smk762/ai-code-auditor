"""Regression tests for auditor/evidence.py audit packs."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from auditor import evidence, validator


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def host_python(monkeypatch):
    monkeypatch.setattr(validator, "ensure_test_venv", lambda root, cache_dir, extra=None: (Path(sys.executable), ""))


BASE = {
    "svc/requirements.txt": "",
    "svc/app/__init__.py": "",
    "svc/app/calc.py": "def add(a, b):\n    return a - b\n",
}


def _fresh(base: Path, tmp_path: Path):
    target = tmp_path / "repro_base"

    def make() -> Path:
        if not target.exists():
            shutil.copytree(base, target)
        return target
    return make


def test_python_repro_through_a_symlinked_tree(tmp_path: Path, host_python) -> None:  # 0ce5198c62
    base, real = tmp_path / "base", tmp_path / "patched"
    _write(base, BASE)
    _write(real, {**BASE, "svc/app/calc.py": "def add(a, b):\n    return a + b\n",
                  "svc/tests/test_calc.py": "from app.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"})
    link = tmp_path / "tree-link"
    link.symlink_to(real, target_is_directory=True)
    res = evidence.python_repro(link, _fresh(base, tmp_path), ["svc/tests/test_calc.py"], cache_dir=tmp_path / "c")
    assert res.repro == ["svc/tests/test_calc.py::test_add"], res.notes


def test_node_repro_skips_a_test_outside_its_project_with_a_note(tmp_path: Path, monkeypatch) -> None:  # 0ce5198c62
    pkg = json.dumps({"name": "w", "version": "1.0.0", "scripts": {"test": "vitest run"}})
    files = {"web/package.json": pkg, "web/package-lock.json": "{}", "web/src/sum.ts": "x\n"}
    base, patched = tmp_path / "base", tmp_path / "patched"
    _write(base, files)
    _write(patched, {**files, "web/src/ok.test.ts": "it('ok')\n"})
    outside = tmp_path / "elsewhere.test.ts"
    outside.write_text("it('x')\n", encoding="utf-8")
    (patched / "web/src/escape.test.ts").symlink_to(outside)
    monkeypatch.setattr(evidence.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("auditor.validate_node.ensure_node_modules",
                        lambda root, cache: ((tmp_path / "nm").mkdir(exist_ok=True) or tmp_path / "nm", ""))
    seen: list[list[str]] = []

    def fake_vitest(root: Path, files: list[str], timeout_s: int):
        seen.append(files)
        return {"src/ok.test.ts::ok": "passed"}, ""
    monkeypatch.setattr(evidence, "_vitest", fake_vitest)
    res = evidence.node_repro(patched, _fresh(base, tmp_path), ["web/src/escape.test.ts", "web/src/ok.test.ts"],
                              cache_dir=tmp_path / "nc")
    assert seen and all(f == ["src/ok.test.ts"] for f in seen)
    assert any("escape.test.ts" in n and "outside" in n for n in res.notes), res.notes
