from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from conductor.briefing import build_briefing
from conductor.publish import md_passes_tts_rules, publish_status
from conductor.schemas import (
    DecisionNeeded,
    DraftPr,
    FindingsSummary,
    MithrandirStatus,
    RestWindowInfo,
    TopFinding,
    derive_quiet_exempt,
    derive_requires_ack,
    derive_severity,
    validate_status_dict,
)


def _sample_status(
    *,
    ended_reason: str = "completed",
    decisions: list[DecisionNeeded] | None = None,
    base: str = "http://192.168.1.198:8765",
) -> MithrandirStatus:
    return MithrandirStatus(
        run_id="rest-test-001",
        generated_at=datetime.now(timezone.utc).isoformat(),
        ended_reason=ended_reason,  # type: ignore[arg-type]
        summary_one_liner="Rest session complete: 2 confirmed, 1 draft PR.",
        speakable=True,
        latest_url=f"{base}/reports/mithrandir/LATEST.json",
        latest_md_url=f"{base}/reports/mithrandir/LATEST.md",
        window=RestWindowInfo(start="00:30", end="06:00"),
        repos=["ai-code-auditor"],
        scanner_counts={"semgrep": 1, "gitleaks": 0, "osv": 0},
        findings=FindingsSummary(
            confirmed=2,
            dismissed=1,
            top=[
                TopFinding(
                    repo="ai-code-auditor",
                    severity="HIGH",
                    title="Missing auth on admin route",
                    pr_url="https://github.com/smk762/ai-code-auditor/pull/42",
                )
            ],
        ),
        draft_prs=[
            DraftPr(
                url="https://github.com/smk762/ai-code-auditor/pull/42",
                title="Fix admin auth",
                severity="HIGH",
                repo="ai-code-auditor",
                action="review",
            )
        ],
        decisions_needed=list(decisions or []),
        next_rest_window="tomorrow 00:30",
    )


def test_schema_validation_ok():
    status = _sample_status()
    md, speakable = build_briefing(status)
    status.body_md = md
    status.speakable = speakable
    errors = validate_status_dict(status.to_dict())
    assert errors == []


def test_clean_completed_no_ack():
    status = _sample_status(ended_reason="completed", decisions=[])
    d = status.to_dict()
    assert d["requires_ack"] is False
    assert d["quiet_exempt"] is False
    assert d["severity"] in ("info", "notice")
    assert d["source"] == "ai-code-auditor"
    assert d["event_id"] == "auditor-rest-rest-test-001"


def test_escalation_requires_ack_and_quiet_exempt():
    status = _sample_status(
        ended_reason="escalation",
        decisions=[DecisionNeeded(id="d1", summary="Confirm auth middleware scope")],
    )
    d = status.to_dict()
    assert d["requires_ack"] is True
    assert d["severity"] == "critical"
    assert d["quiet_exempt"] is True


def test_derive_helpers():
    assert derive_requires_ack("completed", []) is False
    assert derive_requires_ack("completed", [DecisionNeeded(id="x", summary="y")]) is True
    assert derive_severity("completed") == "info"
    assert derive_quiet_exempt("info", False) is False
    assert derive_quiet_exempt("critical", True) is True


def test_summary_one_liner_trimmed():
    status = _sample_status()
    status.summary_one_liner = "x" * 200
    d = status.to_dict()
    assert len(d["summary_one_liner"]) <= 120


def test_atomic_publish(tmp_path: Path):
    status = _sample_status()
    md, speakable = build_briefing(status)
    status.speakable = speakable
    result = publish_status(status, md, output_dir=tmp_path)
    assert result.json_path.exists()
    assert result.md_path.exists()
    assert result.archive_json is not None and result.archive_json.exists()
    loaded = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert loaded["source"] == "ai-code-auditor"
    assert loaded["body_md"].strip()
    assert loaded["event_id"]
    assert "requires_ack" in loaded and "quiet_exempt" in loaded
    ok, reasons = md_passes_tts_rules(result.markdown)
    assert ok, reasons


def test_tts_rejects_tables_and_urls():
    bad = "Outcome completed. See https://example.com/foo for details. | a | b |"
    ok, reasons = md_passes_tts_rules(bad)
    assert not ok
    assert reasons
