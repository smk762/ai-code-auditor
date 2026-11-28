"""conductor.webhook: a webhook URL can carry its credential, so it never reaches a log or a result."""

from __future__ import annotations

import json

import pytest
import requests

SECRET_URL = "https://hooks.example.com/services/T000/B000/s3cretTok?token=q5ecret"


def test_a_failing_webhook_never_logs_or_returns_its_credential(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from conductor import webhook

    def boom(url, **kw):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='hooks.example.com', port=443): Max retries exceeded with url: "
            f"/services/T000/B000/s3cretTok?token=q5ecret ({url})"
        )

    monkeypatch.setattr(webhook.requests, "post", boom)
    with caplog.at_level("WARNING"):
        out = webhook.notify_mithrandir(webhook_url=SECRET_URL, status={"type": "x"})
    assert out["sent"] is False and out["url"] == "https://hooks.example.com/…"
    leaked = json.dumps(out) + caplog.text
    assert "s3cretTok" not in leaked and "q5ecret" not in leaked and "hooks.example.com" in leaked


def test_an_http_error_logs_only_the_host(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from conductor import webhook

    class Resp:
        status_code = 403
        text = "forbidden"

    monkeypatch.setattr(webhook.requests, "post", lambda url, **kw: Resp())
    with caplog.at_level("WARNING"):
        out = webhook.notify_mithrandir(webhook_url=SECRET_URL, status={"type": "x"})
    assert out["status_code"] == 403 and "s3cretTok" not in json.dumps(out) + caplog.text


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("http://m:8400/api/companion/events", "http://m:8400/…"),
        ("https://u:pw@h.example/x?k=v", "https://h.example/…"),
        ("http://h", "http://h"),
        ("nope", "<webhook url>"),
    ],
    ids=["path", "userinfo", "bare", "garbage"],
)
def test_redact_url(url: str, shown: str) -> None:
    from conductor.webhook import redact_url

    assert redact_url(url) == shown
