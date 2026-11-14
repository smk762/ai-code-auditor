from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import ui_runs
from auditor.db import AuditRun, FindingRecord, RepoRun

NOW = datetime.now(timezone.utc)


def _run(s, run_id, pipeline="repo-audit", status="success", hours_ago=0.0, repos=("x",), **meta):
    s.add(AuditRun(run_id=run_id, pipeline=pipeline, status=status,
                   started_at=NOW - timedelta(hours=hours_ago),
                   finished_at=NOW - timedelta(hours=hours_ago) + timedelta(minutes=5),
                   metadata_json={"scanned_repos": list(repos), **meta}))


def _finding(s, run_id, title, repo="x", sev="HIGH", line=1, file="a.py", ftype="bug", source="llm"):
    s.add(FindingRecord(run_id=run_id, repo=repo, file_path=file, line=line, finding_type=ftype,
                        severity=sev, title=title, source=source, created_at=NOW))


@pytest.fixture
def history(memdb):
    """Nightly single-repo audits alternating between repos x and y, plus noise."""
    with memdb() as s:
        _run(s, "x-1", hours_ago=48, repos=("x",))
        _finding(s, "x-1", "kept", line=10)
        _finding(s, "x-1", "gone", sev="CRITICAL")
        _run(s, "y-1", hours_ago=24, repos=("y",))
        _finding(s, "y-1", "y thing", repo="y")
        _run(s, "x-failed", status="failed", hours_ago=12, repos=("x",))
        _run(s, "x-2", status="completed", hours_ago=1, repos=("x",))
        _finding(s, "x-2", "kept", line=14)  # same finding, line shifted
        _finding(s, "x-2", "fresh", sev="LOW")
        _finding(s, "x-2", "fresh too", sev="HIGH", file="b.py", source="bandit")
        _run(s, "eco-1", pipeline="ecosystem-audit", hours_ago=2, repos=("x", "y"))
        s.add(RepoRun(run_id="x-2", repo_name="x", status="completed", attempts=2, error_message="",
                      updated_at=NOW))
    return memdb


# ── runs list ────────────────────────────────────────────────────────────────

def test_runs_data_lists_all_pipelines_with_facets(history) -> None:
    d = ui_runs.runs_data()
    assert [r["run_id"] for r in d["runs"]] == ["x-2", "eco-1", "x-failed", "y-1", "x-1"]
    assert d["pipelines"] == {"ecosystem-audit": 1, "repo-audit": 4}
    assert d["statuses"] == {"completed": 1, "failed": 1, "success": 3}
    assert {r["run_id"]: r["findings_stored"] for r in d["runs"]}["x-2"] == 3
    assert d["runs"][0]["pipeline"] == "repo-audit"


def test_runs_data_filters(history) -> None:
    assert [r["run_id"] for r in ui_runs.runs_data(pipeline="ecosystem-audit")["runs"]] == ["eco-1"]
    assert {r["run_id"] for r in ui_runs.runs_data(status="failed,completed")["runs"]} == {"x-2", "x-failed"}
    assert len(ui_runs.runs_data(limit=2)["runs"]) == 2


# ── run summary ──────────────────────────────────────────────────────────────

def test_run_summary_compares_with_last_finished_run_of_same_repos(history, tmp_path) -> None:
    d = ui_runs.run_summary("x-2", tmp_path)
    # baseline skips y-1 (other repo) and x-failed (not finished-ok)
    assert d["previous"]["run_id"] == "x-1"
    f = d["findings"]
    assert f["stored"] == 3
    assert f["by_severity"]["HIGH"] == 2 and f["by_severity"]["LOW"] == 1
    assert f["delta"]["new"] == 2 and f["delta"]["fixed"] == 1
    assert [x["title"] for x in f["delta"]["new_findings"]] == ["fresh too", "fresh"]  # most severe first
    assert [x["title"] for x in f["delta"]["fixed_findings"]] == ["gone"]
    assert f["delta"]["by_severity"] == {"CRITICAL": -1, "HIGH": 1, "MEDIUM": 0, "LOW": 1, "INFO": 0}
    assert f["by_repo"] == {"x": {"HIGH": 2, "LOW": 1}}
    assert d["repos"] == [{"repo_name": "x", "status": "completed", "attempts": 2, "error_message": "",
                           "updated_at": d["repos"][0]["updated_at"]}]
    assert d["combined_report_url"] == "/audit/reports/x-2"


def test_run_summary_without_baseline_or_unknown(history, tmp_path) -> None:
    d = ui_runs.run_summary("x-1", tmp_path)
    assert d["previous"] is None and "delta" not in d["findings"]
    assert ui_runs.run_summary("nope", tmp_path) is None
    assert ui_runs.run_summary("x-failed", tmp_path)["combined_report_url"] is None


def test_run_summary_flags_reports_from_other_runs(history, tmp_path) -> None:
    mine, other = tmp_path / "nightly_repo_report.md", tmp_path / "security_report.md"
    mine.write_text("# r")
    other.write_text("# s")
    in_window = (NOW - timedelta(hours=1) + timedelta(minutes=2)).timestamp()
    os.utime(mine, (in_window, in_window))
    os.utime(other, (NOW.timestamp(), NOW.timestamp()))  # written after x-2 finished (+ slack)
    (tmp_path / "ecosystem_report.md").write_text("# not a repo-audit report")
    reports = {r["name"]: r for r in ui_runs.run_summary("x-2", tmp_path)["reports"]}
    assert set(reports) == {"nightly_repo_report.md", "security_report.md"}
    assert reports["nightly_repo_report.md"]["from_this_run"] is True
    assert reports["security_report.md"]["from_this_run"] is False
    assert reports["nightly_repo_report.md"]["url"] == "/ui/reports/nightly_repo_report.md"


# ── findings ─────────────────────────────────────────────────────────────────

def test_findings_default_run_is_newest_finished_run_with_findings(history) -> None:
    d = ui_runs.findings_data()
    assert d["run"]["run_id"] == "x-2"  # eco-1 is newer-ish but stores no findings
    assert d["total"] == 3 and d["filtered_total"] == 3
    assert d["matrix"] == {"x": {"CRITICAL": 0, "HIGH": 2, "MEDIUM": 0, "LOW": 1, "INFO": 0}}
    assert d["facets"] == {"repos": ["x"], "sources": ["bandit", "llm"], "types": ["bug"]}
    assert d["delta"] == {"new": 2, "fixed": 1, "reworded": 0}
    assert {f["title"]: f["is_new"] for f in d["findings"]} == {"kept": False, "fresh": True, "fresh too": True}


def test_findings_filters_and_change(history) -> None:
    def titles(**kw):
        return [f["title"] for f in ui_runs.findings_data("x-2", **kw)["findings"]]

    assert titles(severity="low,critical") == ["fresh"]
    assert titles(source="bandit") == ["fresh too"]
    assert titles(q="B.PY") == ["fresh too"]
    assert titles(change="new") == ["fresh too", "fresh"]
    assert titles(change="fixed") == ["gone"]
    assert titles(repo="y") == []
    d = ui_runs.findings_data("x-2", limit=1)
    assert d["filtered_total"] == 3 and len(d["findings"]) == 1


def test_findings_unknown_run_and_empty_db(history, memdb) -> None:
    assert ui_runs.findings_data("nope") is None
    assert ui_runs.findings_data("y-1", change="new")["findings"] == []  # no baseline → nothing "new"


def test_findings_empty_db(memdb) -> None:
    d = ui_runs.findings_data()
    assert d["run"] is None and d["findings"] == []


def _f(title, line=1, file="/srv/mirrors/x/app.py", ftype="bug", repo="x"):
    return {"repo": repo, "file_path": file, "path": file, "line": line, "type": ftype,
            "severity": "HIGH", "title": title, "source": "llm"}


def test_delta_pairs_reworded_findings_in_same_file_and_type() -> None:
    prev = [
        _f("Minor-exposed image block on /image is bypassed by omitting scan_id", line=380),
        _f("--output is silently ignored for directory captioning", line=10),
        _f("Move mode can write through a stale symlink", line=4),
        _f("Unrestricted URL fetch with redirect following enables SSRF", ftype="security"),
    ]
    cur = [
        _f("/image serves minor-exposed originals when scan_id is omitted", line=381),  # near + 0.2+
        _f("--output silently ignored in directory mode", line=32),  # far, but 0.3+
        _f("Symlink export deletes the source file", line=6),  # near, unrelated words
        _f("Unrestricted URL fetch with redirect following enables SSRF", ftype="bug"),  # other type
    ]
    d = ui_runs._delta(cur, prev, set())
    assert d["reworded"] == 2
    assert [f["title"] for f in d["new"]] == ["Symlink export deletes the source file",
                                              "Unrestricted URL fetch with redirect following enables SSRF"]
    assert [f["title"] for f in d["fixed"]] == ["Move mode can write through a stale symlink",
                                                "Unrestricted URL fetch with redirect following enables SSRF"]


def test_delta_pairs_each_finding_once_best_match_first() -> None:
    prev = [_f("token sent to caller controlled host")]
    cur = [_f("token sent to caller controlled host header"), _f("token sent to caller controlled host")]
    d = ui_runs._delta(cur, prev, set())
    assert d["reworded"] == 0  # the exact-title match wins; the other stays new
    assert [f["title"] for f in d["new"]] == ["token sent to caller controlled host header"]


def test_display_path_is_repo_relative() -> None:
    assert ui_runs._display_path("argus-curator", "/home/u/mirrors/argus-curator/src/app.py") == "src/app.py"
    assert ui_runs._display_path("x", "relative/app.py") == "relative/app.py"


# ── routes ───────────────────────────────────────────────────────────────────

def test_pages_render_in_shell(client: TestClient) -> None:
    for path, active in (("/ui/runs", "Runs"), ("/ui/runs/x-2", "Runs"), ("/ui/findings", "Findings")):
        r = client.get(path)
        assert r.status_code == 200
        assert f'aria-current="page">{active}</a>' in r.text
        assert "<!--#nav-->" not in r.text


def test_json_endpoints(client: TestClient, history) -> None:
    assert client.get("/audit/runs.json?pipeline=repo-audit").json()["filters"]["pipeline"] == "repo-audit"
    s = client.get("/audit/runs/x-2/summary.json")
    assert s.status_code == 200 and s.json()["previous"]["run_id"] == "x-1"
    assert client.get("/audit/runs/nope/summary.json").status_code == 404
    f = client.get("/audit/findings.json?run_id=x-2&change=new")
    assert f.status_code == 200 and f.json()["filtered_total"] == 2
    assert client.get("/audit/findings.json?run_id=nope").status_code == 404
    assert client.get("/audit/findings.json?change=bogus").status_code == 422


def test_report_page_renders_markdown_and_refuses_other_paths(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "security_report.md").write_text("# Security\n\n<script>x</script>\n\n| a |\n|:-:|\n| 1 |\n")
    (tmp_path.parent / "secret.md").write_text("# nope")
    r = client.get("/ui/reports/security_report.md")
    assert r.status_code == 200
    assert '<h1 id="security">Security</h1>' in r.text and "&lt;script&gt;" in r.text
    assert '<td class="align-center">1</td>' in r.text
    assert client.get("/ui/reports/missing.md").status_code == 404
    assert client.get("/ui/reports/..%2Fsecret.md").status_code == 404


def test_combined_report_accepts_pipeline_success_status(client: TestClient, tmp_path: Path) -> None:
    """Timer/CLI runs end as "success"; the endpoint used to accept only "completed"."""
    from auditor.db import db_session

    with db_session() as s:
        s.add(AuditRun(run_id="cli-run", pipeline="ecosystem-audit", status="success",
                       started_at=NOW, finished_at=NOW, metadata_json={}))
    (tmp_path / "run_cli-run.json").write_text("{}")
    (tmp_path / "ecosystem_report.md").write_text("# Eco")
    r = client.get("/audit/reports/cli-run")
    assert r.status_code == 200 and "# Eco" in r.text
