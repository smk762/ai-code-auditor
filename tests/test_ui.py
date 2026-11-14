from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import ui
from auditor.db import AuditRun, FindingRecord
from auditor.spend_ledger import get_ledger


# ── nav + pages ──────────────────────────────────────────────────────────────

def test_nav_marks_active_page() -> None:
    nav = ui.nav_html("/audit/usage")
    assert '<a href="/audit/usage" aria-current="page">Usage</a>' in nav
    assert '<a href="/">Overview</a>' in nav


def test_every_page_uses_shared_shell() -> None:
    for page in ("index.html", "usage_dashboard.html"):
        raw = (ui.STATIC_DIR / page).read_text(encoding="utf-8")
        assert ui.NAV_MARKER in raw
        assert "/audit/static/ui.css" in raw and "/audit/static/ui.js" in raw
        assert "--surface-0:" not in raw  # tokens live only in ui.css
        rendered = ui.render_page(page, "/")
        assert ui.NAV_MARKER not in rendered and 'class="ui-nav"' in rendered


def test_landing_and_static_routes(client: TestClient) -> None:
    for path in ("/", "/ui"):
        r = client.get(path)
        assert r.status_code == 200 and "Ecosystem overview" in r.text
        assert '<a href="/" aria-current="page">Overview</a>' in r.text
    usage = client.get("/audit/usage")
    assert 'aria-current="page">Usage</a>' in usage.text
    css = client.get("/audit/static/ui.css")
    assert css.status_code == 200 and "--surface-0" in css.text
    assert client.get("/audit/static/ui.js").status_code == 200


# ── markdown ─────────────────────────────────────────────────────────────────

def test_markdown_escapes_html_and_unsafe_links() -> None:
    out = ui.render_markdown("<script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://example.com)")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert 'href="javascript:' not in out
    assert '<a href="https://example.com">ok</a>' in out


def test_markdown_tables_use_classes_not_inline_style() -> None:
    out = ui.render_markdown("| a | b | c |\n|:-|:-:|-|\n| 1 | 2 | 3 |\n")
    assert "style=" not in out
    assert '<th class="align-left">a</th>' in out
    assert '<td class="align-center">2</td>' in out
    assert "<td>3</td>" in out


def test_markdown_heading_ids_are_unique() -> None:
    out = ui.render_markdown("# Key entry points\n\n## Setup\n\n## Setup\n")
    assert '<h1 id="key-entry-points">' in out
    assert '<h2 id="setup">' in out and '<h2 id="setup-1">' in out


# ── summary ──────────────────────────────────────────────────────────────────

def test_summary_reads_runs_findings_packs_and_spend(tmp_path: Path, memdb, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_BUDGET_TOTAL_USD", "100")
    now = datetime.now(timezone.utc)
    with memdb() as s:
        # ecosystem runs store no findings rows; repo audits do
        s.add(AuditRun(run_id="eco-1", pipeline="ecosystem-audit", status="success",
                       started_at=now - timedelta(hours=1), metadata_json={"scanned_repos": ["a", "b"]}))
        s.add(AuditRun(run_id="repo-old", pipeline="repo-audit", status="success",
                       started_at=now - timedelta(days=2), metadata_json={}))
        s.add(AuditRun(run_id="repo-1", pipeline="repo-audit", status="completed",  # API-started run
                       started_at=now - timedelta(hours=3),
                       metadata_json={"findings": 3, "duration_ms": 5000, "scanned_repos": ["a", "b"]}))
        s.add(AuditRun(run_id="repo-live", pipeline="repo-audit", status="running", started_at=now))
        for sev in ("HIGH", "HIGH", "low"):
            s.add(FindingRecord(run_id="repo-1", repo="a", file_path="x.py", line=1, finding_type="t",
                                severity=sev, title="t", source="bandit", created_at=now))
        for run_id in ("repo-old", "repo-live"):
            s.add(FindingRecord(run_id=run_id, repo="a", file_path="x.py", line=1, finding_type="t",
                                severity="CRITICAL", title="t", source="bandit", created_at=now))
    packs = tmp_path / "agent_packs"
    packs.mkdir()
    (packs / "INDEX.json").write_text(json.dumps(
        {"count": 4, "updated_at": "2026-11-14T00:00:00Z", "status_counts": {"open": 3, "resolved": 1}, "packs": []}))
    get_ledger().record_manual("eval", 12.5, note="evals")

    d = ui.summary_data(tmp_path)
    assert d["errors"] == []
    assert d["last_run"]["run_id"] == "repo-live"  # newest run of any pipeline
    # newest *finished* run with findings stored, counting the API's "completed" status
    assert d["findings"]["run_id"] == "repo-1" and d["findings"]["pipeline"] == "repo-audit"
    assert d["findings"]["repos"] == 2
    assert d["findings"]["total"] == 3
    assert d["findings"]["by_severity"] == {"CRITICAL": 0, "HIGH": 2, "MEDIUM": 0, "LOW": 1, "INFO": 0}
    assert d["packs"]["total"] == 4
    assert d["packs"]["status_counts"]["open"] == 3 and d["packs"]["status_counts"]["claimed"] == 0
    assert d["spend"]["spent"] == pytest.approx(12.5) and d["spend"]["cap"] == 100


def test_summary_degrades_per_section(tmp_path: Path, monkeypatch) -> None:
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(ui, "_db_session", broken)
    d = ui.summary_data(tmp_path)
    assert d["last_run"] is None and d["findings"] is None
    assert d["packs"]["total"] == 0  # no pack index yet is not an error
    assert d["spend"] is not None
    assert [e.split(":")[0] for e in d["errors"]] == ["runs", "findings"]
    assert d["errors"][0].startswith("runs: RuntimeError")


def test_summary_endpoint(client: TestClient, memdb) -> None:
    r = client.get("/audit/summary.json")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"last_run", "findings", "packs", "spend", "errors"}
    assert body["errors"] == []


def test_pack_savings_page_in_shell(client: TestClient) -> None:
    r = client.get("/ui/pack-savings")
    assert r.status_code == 200
    assert 'aria-current="page">Pack savings</a>' in r.text
    assert "/audit/pack-savings.json" in r.text
