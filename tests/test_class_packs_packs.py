"""Regression tests for class_packs audit packs (pack severity, export failures)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from auditor import class_packs as cp


def _child(pid: str, sev: str, path: str) -> dict:
    return {"work_item_id": pid, "kind": "nightly", "status": "open", "priority": {"severity": sev, "score": 0.5},
            "repo": {"name": "r1"}, "location": {"file_path": path, "line": 1},
            "narrative": {"title": f"{pid} lets the caller pick the URL"},
            "governance": {"finding_class": {"id": "ssrf", "guard": "helper"}}}


def test_class_pack_severity_is_the_worst_child_even_a_non_code_one() -> None:
    children = [_child("pack-docs", "CRITICAL", "docs/guide.md"),
                _child("pack-a", "LOW", "app/a.py"), _child("pack-b", "LOW", "app/b.py")]
    pack = cp.build("ssrf", children)
    assert pack.priority["severity"] == "CRITICAL"
    # code findings still lead the examples
    assert pack.narrative["description"].index("pack-a") < pack.narrative["description"].index("pack-docs")


def test_export_timeout_returns_false(tmp_path: Path, monkeypatch) -> None:
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git archive", timeout=300)

    monkeypatch.setattr(cp.subprocess, "run", hang)
    assert cp._export(tmp_path, "dev", tmp_path / "out") is False


def test_export_missing_mirror_returns_false(tmp_path: Path) -> None:
    assert cp._export(tmp_path / "gone", "dev", tmp_path / "out") is False
