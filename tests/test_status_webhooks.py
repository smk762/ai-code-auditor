"""Status webhooks at wrap-up: mithrandir unchanged, extra targets from AI_AUDIT_STATUS_WEBHOOKS."""

from __future__ import annotations

import json

import pytest
import requests

from conductor import webhook

STATUS = {
    "schema_version": 1,
    "source": "ai-code-auditor",
    "event_id": "auditor-rest-r1",
    "severity": "notice",
    "summary_one_liner": "Rest completed: 2 confirmed, 1 draft PRs",
    "latest_md_url": "http://audit:8765/reports/mithrandir/LATEST.md",
    "run_id": "r1",
}


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code
        self.text = ""


@pytest.fixture
def posts(monkeypatch):
    calls: list[dict] = []
    codes: dict[str, object] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        code = codes.get(url, 200)
        if isinstance(code, Exception):
            raise code
        return _Resp(code)

    monkeypatch.setattr(webhook.requests, "post", fake_post)
    monkeypatch.delenv(webhook.STATUS_WEBHOOKS_ENV, raising=False)
    return calls, codes


def _targets(monkeypatch, targets):
    monkeypatch.setenv(webhook.STATUS_WEBHOOKS_ENV, json.dumps(targets))


def test_mithrandir_still_gets_the_envelope_without_auth(posts, monkeypatch):
    calls, _ = posts
    monkeypatch.setenv("AI_AUDIT_MITHRANDIR_WEBHOOK_URL", "http://m:8400/api/companion/events")
    out = webhook.notify_mithrandir(status=STATUS)
    assert out["sent"] is True and out["url"] == "http://m:8400/…"  # the path may hold a credential
    (call,) = calls
    assert call["headers"] is None
    assert call["json"]["summary_one_liner"] == STATUS["summary_one_liner"]


@pytest.mark.parametrize("value", [None, "", "  "])
def test_no_mithrandir_webhook_is_sent_unless_its_url_is_set(posts, monkeypatch, value):
    calls, _ = posts
    if value is None:
        monkeypatch.delenv(webhook.MITHRANDIR_WEBHOOK_ENV, raising=False)
    else:
        monkeypatch.setenv(webhook.MITHRANDIR_WEBHOOK_ENV, value)
    assert webhook.mithrandir_webhook_url() == ""
    out = webhook.notify_mithrandir(status=STATUS)
    assert out == {"sent": False, "skipped": True, "reason": "webhook_unset"} and calls == []


def test_payload_carries_the_argus_summary_and_link():
    payload = webhook.build_payload(STATUS)
    assert payload["summary"] == STATUS["summary_one_liner"]
    assert payload["link"] == STATUS["latest_md_url"]
    assert payload["source"] == "ai-code-auditor" and payload["event_id"] == "auditor-rest-r1"
    assert "summary" not in STATUS  # the published status is not mutated


def test_no_extra_targets_by_default(posts):
    calls, _ = posts
    assert webhook.notify_extra_targets(status=STATUS) == []
    assert calls == []


def test_extra_target_gets_the_bearer_token_from_its_env(posts, monkeypatch):
    calls, _ = posts
    _targets(monkeypatch, [{"name": "scout", "url": "http://s:8402/api/events/ai-code-auditor", "token_env": "TOK"}])
    monkeypatch.setenv("TOK", "s3cret")
    (result,) = webhook.notify_extra_targets(status=STATUS)
    assert result == {
        "name": "scout",
        "sent": True,
        "skipped": False,
        "status_code": 200,
        "url": "http://s:8402/…",
    }
    (call,) = calls
    assert call["headers"] == {"Authorization": "Bearer s3cret"}
    assert call["json"]["summary"] == STATUS["summary_one_liner"]
    assert "s3cret" not in json.dumps(result)


def test_a_target_whose_token_is_unset_is_skipped_not_sent_bare(posts, monkeypatch):
    calls, _ = posts
    _targets(monkeypatch, [{"name": "scout", "url": "http://s/api/events/x", "token_env": "TOK"}])
    monkeypatch.delenv("TOK", raising=False)
    (result,) = webhook.notify_extra_targets(status=STATUS)
    assert result["skipped"] is True and result["reason"] == "token_unset"
    assert calls == []


def test_a_failing_target_never_stops_the_others(posts, monkeypatch):
    calls, codes = posts
    _targets(
        monkeypatch,
        [
            {"name": "down", "url": "http://down/x"},
            {"name": "refuses", "url": "http://refuses/x"},
            {"name": "ok", "url": "http://ok/x"},
        ],
    )
    codes["http://down/x"] = requests.ConnectionError("refused")
    codes["http://refuses/x"] = 401
    results = webhook.notify_extra_targets(status=STATUS)
    assert [r["name"] for r in results] == ["down", "refuses", "ok"]
    assert [r["sent"] for r in results] == [False, False, True]
    assert results[1]["status_code"] == 401
    assert len(calls) == 3


@pytest.mark.parametrize(
    "raw",
    ["not json", '{"url": "http://x"}', '[1, {"url": "ftp://x"}, {"name": "no url"}]'],
)
def test_bad_config_is_reported_and_never_fatal(posts, monkeypatch, raw):
    calls, _ = posts
    monkeypatch.setenv(webhook.STATUS_WEBHOOKS_ENV, raw)
    results = webhook.notify_extra_targets(status=STATUS)
    assert results and all(r["sent"] is False and r["reason"] == "bad_target" for r in results)
    assert calls == []


def test_good_entries_survive_a_bad_one(posts, monkeypatch):
    calls, _ = posts
    monkeypatch.setenv(webhook.STATUS_WEBHOOKS_ENV, '[{"url": "nope"}, {"name": "ok", "url": "http://ok/x"}]')
    results = webhook.notify_extra_targets(status=STATUS)
    assert [r.get("reason") for r in results] == ["bad_target", None]
    assert [c["url"] for c in calls] == ["http://ok/x"]


def test_wrap_up_sends_to_mithrandir_and_the_extra_targets(posts, monkeypatch, tmp_path):
    from conductor.schemas import FindingsSummary, MithrandirStatus, RestWindowInfo
    from conductor.wrapup import wrap_up

    calls, _ = posts
    monkeypatch.setenv("AI_AUDIT_MITHRANDIR_WEBHOOK_URL", "http://m/events")
    _targets(monkeypatch, [{"name": "scout", "url": "http://s/api/events/ai-code-auditor"}])
    monkeypatch.setenv("AI_AUDIT_PR_INTEL", "0")
    status = MithrandirStatus(
        run_id="r2",
        generated_at="2026-11-22T00:00:00+00:00",
        ended_reason="completed",
        summary_one_liner="Rest completed",
        speakable=True,
        latest_url="http://a/LATEST.json",
        latest_md_url="http://a/LATEST.md",
        window=RestWindowInfo(start="00:30", end="06:00"),
        findings=FindingsSummary(),
    )
    out = wrap_up(status, output_dir=tmp_path, send_webhook=True, ingest_rag=False)
    assert out["webhook"]["sent"] is True
    assert [r["name"] for r in out["webhooks"]] == ["scout"] and out["webhooks"][0]["sent"] is True
    assert [c["url"] for c in calls] == ["http://m/events", "http://s/api/events/ai-code-auditor"]
    assert calls[1]["json"]["event_id"] == "auditor-rest-r2"


def test_wrap_up_without_webhooks_sends_nothing(posts, monkeypatch, tmp_path):
    from conductor.schemas import FindingsSummary, MithrandirStatus, RestWindowInfo
    from conductor.wrapup import wrap_up

    calls, _ = posts
    _targets(monkeypatch, [{"name": "scout", "url": "http://s/x"}])
    monkeypatch.setenv("AI_AUDIT_PR_INTEL", "0")
    status = MithrandirStatus(
        run_id="r3",
        generated_at="2026-11-22T00:00:00+00:00",
        ended_reason="completed",
        summary_one_liner="Rest completed",
        speakable=True,
        latest_url="http://a/LATEST.json",
        latest_md_url="http://a/LATEST.md",
        window=RestWindowInfo(start="00:30", end="06:00"),
        findings=FindingsSummary(),
    )
    out = wrap_up(status, output_dir=tmp_path, send_webhook=False, ingest_rag=False)
    assert out["webhooks"] == [] and calls == []
