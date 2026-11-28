"""conductor.draft_pr: the public PR body leaks no host detail and keeps its JSON block whole; branch
cleanup stays best effort (#128)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = "/home/someone/mirrors/argus-x"
BASE = "http://10.9.8.7:8765"


def _body(finding: dict, summary: str = "fixed it") -> str:
    from conductor.draft_pr import finding_to_pr_body

    return finding_to_pr_body(finding, summary, pack_id="pack-x-1", repo_path=ROOT)


def _finding_json(body: str) -> dict:
    m = re.search(r"### Finding\n(`{3,})json\n(.*?)\n\1\n", body, re.S)
    assert m, body
    return json.loads(m.group(2))


def test_worker_summary_gets_the_same_redaction_as_the_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_PUBLIC_BASE_URL", BASE)
    summary = f"Patched {ROOT}/app/up.py; see {BASE}/audit/packs/pack-x-1 and /home/someone/notes/plan.md"
    body = _body({"title": "t"}, summary)
    head = body.split("### Finding")[0]
    assert "app/up.py" in head and "plan.md" in head
    assert "/home/someone" not in body and "10.9.8.7" not in body


def test_host_paths_in_free_text_and_nested_fields_are_made_relative() -> None:
    finding = {
        "title": f"Traversal in {ROOT}/app/up.py",
        "description": f"see {ROOT}/app/up.py:12 and /home/someone/mirrors/other/lib.py",
        "related": [{"file": f"{ROOT}/app/b.py", "note": f"called from {ROOT}/app/c.py"}],
        "snippets": [f"# {ROOT}/app/d.py"],
        "line": 12,
    }
    body = _body(finding)
    assert "/home/someone" not in body
    out = _finding_json(body)
    assert out["description"] == "see app/up.py:12 and lib.py"
    assert out["related"] == [{"file": "app/b.py", "note": "called from app/c.py"}]
    assert out["snippets"] == ["# app/d.py"] and out["line"] == 12


def test_backticks_in_a_field_cannot_close_the_json_fence() -> None:
    finding = {"title": "t", "evidence": "```\n[click](http://evil.example) @someone\n```", "tail": "after"}
    body = _body(finding)
    assert _finding_json(body) == finding
    fence = re.search(r"### Finding\n(`+)json", body).group(1)
    assert len(fence) > 3


def test_long_fields_are_cut_before_serialising_so_the_json_stays_valid() -> None:
    finding = {f"f{i}": 'x\\"y' * 2000 for i in range(8)}
    body = _body(finding)
    out = _finding_json(body)  # parses: never sliced mid-string or mid-escape
    assert set(out) == set(finding) and all(v.endswith("…") for v in out.values())
    assert len(body) < 4000


def test_branch_cleanup_that_times_out_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from conductor import draft_pr

    monkeypatch.setattr(draft_pr, "_git_out", lambda args, cwd: "abc123")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 1)

    monkeypatch.setattr(draft_pr.subprocess, "run", hang)
    with caplog.at_level("WARNING"):
        draft_pr._delete_pushed_branch(tmp_path, "origin", "repair/x")
    assert "could not delete orphan branch repair/x" in caplog.text
