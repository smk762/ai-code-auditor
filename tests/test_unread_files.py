"""A file the architecture detectors skip is reported as unread (ecosystem/unread.py)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ecosystem.arch_contracts import repo_dependencies
from ecosystem.call_graph_analyzer import analyze_call_graph
from ecosystem.unread import unread_files
from patterns.service_role_violations import detect_competing_providers

BAD = {"syntax": "def broken(:\n", "nul": "x = 1\0\n"}
CALL = 'import os, httpx\nfrom b_svc import x\nB_URL = os.getenv("B_URL")\nhttpx.get(B_URL)\n'
PROVIDERS = "class OneImageProvider: ...\nclass TwoImageProvider: ...\n"


def _detect(files: dict, roots: dict) -> list[bool]:
    rules = {"service_url_env_vars": [{"env_fragment": "B_URL", "service": "b-svc"}], "allowed_calls": []}
    return [bool(repo_dependencies(files, roots, ["a", "b-svc"])), bool(analyze_call_graph(files, rules)),
            bool(detect_competing_providers(files, [{"name": "a", "role": "worker"}]))]


@pytest.mark.parametrize("tail", [*BAD.values()], ids=[*BAD])
def test_every_file_a_detector_goes_blind_on_is_listed(tmp_path: Path, tail: str) -> None:
    f = tmp_path / "src" / "a.py"
    f.parent.mkdir()
    files, roots = {"a": [f], "clean": []}, {"a": str(tmp_path)}
    f.write_text(CALL + PROVIDERS)
    assert _detect(files, roots) == [True, True, True] and unread_files(files, roots) == {}
    f.write_text(CALL + PROVIDERS + tail)
    with pytest.raises((SyntaxError, ValueError)):  # a tail that parses would check nothing
        ast.parse(f.read_text())
    assert _detect(files, roots) == [False, False, False]  # blind, so it must say so
    assert unread_files(files, roots) == {"a": ["src/a.py"]}


def test_a_file_that_is_gone_or_not_python(tmp_path: Path) -> None:
    (tmp_path / "x.ts").write_text("def broken(:")
    files = {"a": [tmp_path / "x.ts", tmp_path / "gone.py", str(tmp_path / "gone.html")]}
    assert unread_files(files, {"a": str(tmp_path)}) == {"a": ["gone.html", "gone.py"]}


def test_the_ecosystem_run_passes_what_it_could_not_read_to_the_gc() -> None:
    src = (Path(__file__).resolve().parents[1] / "pipelines/ecosystem_audit.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "close_unreported_architecture"]
    assert calls and all({k.arg: ast.unparse(k.value) for k in c.keywords}.get("unread") == "unread" for c in calls)
    assert "unread = unread_files(repo_files, repo_roots)" in src
