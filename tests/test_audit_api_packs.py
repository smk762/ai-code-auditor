"""Regression tests for audit-pack fixes in auditor/audit_api.py."""

from __future__ import annotations

import hmac

import pytest
from fastapi.testclient import TestClient

from auditor import audit_api


# ── auth: secrets are compared in constant time ──────────────────────────────

def test_api_token_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "s3cret")
    monkeypatch.setattr(hmac, "compare_digest", spy)
    audit_api._require_mutating_auth(authorization=None, x_auth_code=None, x_api_token="s3cret")
    assert calls == [(b"s3cret", b"s3cret")]
    with pytest.raises(audit_api.HTTPException) as exc:
        audit_api._require_mutating_auth(authorization=None, x_auth_code=None, x_api_token="s3creX")
    assert exc.value.status_code == 401


def test_auth_code_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    cfg = audit_api.load_ecosystem_config()
    cfg.auth_mode = True
    cfg.auth_code_env = "TEST_AUDIT_AUTH_CODE"
    monkeypatch.setattr(audit_api, "load_ecosystem_config", lambda: cfg)
    monkeypatch.setenv("TEST_AUDIT_AUTH_CODE", "code-1")
    monkeypatch.setattr(hmac, "compare_digest", spy)
    audit_api._require_mutating_auth(authorization=None, x_auth_code="code-1", x_api_token=None)
    assert (b"code-1", b"code-1") in calls
