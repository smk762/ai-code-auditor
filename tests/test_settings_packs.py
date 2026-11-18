"""Regression tests for auditor/settings.py: the JWT secret never falls back to a public value."""
from __future__ import annotations

import time
import warnings

import jwt
import pytest

from auditor import auth
from auditor.settings import get_settings

JWT_MIN_SECRET_LEN = 32  # HS256 key floor enforced by auditor.settings


def _token(key: str) -> str:
    s = get_settings()  # issuer/audience/algorithm as the verifier sees them in this environment
    now = int(time.time())
    claims = {"sub": "attacker", "iss": s.jwt_issuer, "aud": s.jwt_audience,
              "exp": now + 600, "roles": ["admin"]}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # PyJWT warns about short HMAC keys
        return jwt.encode(claims, key, algorithm=s.jwt_algorithm)


@pytest.mark.parametrize("configured", [None, "change-me", "short"])
def test_token_forged_with_the_placeholder_is_refused(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("JWT_SECRET", raising=False)
    else:
        monkeypatch.setenv("JWT_SECRET", configured)
    assert get_settings().jwt_secret not in {"change-me", "short", ""}
    for key in ("change-me", "short"):
        with pytest.raises(jwt.InvalidTokenError):
            auth.verify_jwt_token(_token(key))


def test_strong_secret_still_verifies(monkeypatch):
    strong = "s" * JWT_MIN_SECRET_LEN
    monkeypatch.setenv("JWT_SECRET", strong)
    assert get_settings().jwt_secret == strong
    ctx = auth.verify_jwt_token(_token(strong))
    assert ctx.subject == "attacker" and "admin" in ctx.roles


def test_settings_load_without_a_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    s = get_settings()  # startup paths that never use JWT must not fail
    assert len(s.jwt_secret) >= JWT_MIN_SECRET_LEN
