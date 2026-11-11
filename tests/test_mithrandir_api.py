from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from auditor.audit_api import app
from conductor.schemas import FindingsSummary, MithrandirStatus, RestWindowInfo
from conductor.wrapup import wrap_up


def test_latest_endpoints(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(tmp_path))
    status = MithrandirStatus(
        run_id="api-test-1",
        generated_at=datetime.now(timezone.utc).isoformat(),
        ended_reason="completed",
        summary_one_liner="API test clean run.",
        speakable=True,
        latest_url="http://192.168.1.198:8765/reports/mithrandir/LATEST.json",
        latest_md_url="http://192.168.1.198:8765/reports/mithrandir/LATEST.md",
        window=RestWindowInfo(start="00:30", end="06:00"),
        findings=FindingsSummary(),
    )
    wrap_up(status, output_dir=tmp_path, send_webhook=False, ingest_rag=False)
    client = TestClient(app)
    r = client.get("/reports/mithrandir/LATEST.json")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "ai-code-auditor"
    assert body["event_id"]
    assert body["requires_ack"] is False
    assert "body_md" in body
    md = client.get("/reports/mithrandir/LATEST.md")
    assert md.status_code == 200
    assert "rest session" in md.text.lower() or "completed" in md.text.lower()
