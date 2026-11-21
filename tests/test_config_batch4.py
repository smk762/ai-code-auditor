"""YAML flag parsing and an empty ``repos:`` in ``load_ecosystem_config``."""
from __future__ import annotations

import pytest

from auditor.config import load_ecosystem_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AI_AUDIT_AUTH_MODE", raising=False)


def _cfg(tmp_path, body: str):
    p = tmp_path / "ecosystem.yaml"
    p.write_text(body)
    return p


def test_quoted_false_disables_a_repo(tmp_path) -> None:
    cfg = load_ecosystem_config(_cfg(tmp_path, (
        "repos:\n"
        "  - {name: a, path: /a}\n"
        "  - {name: b, path: /b, enabled: \"false\"}\n"
        "  - {name: c, path: /c, enabled: false}\n")))
    assert [r.name for r in cfg.repos] == ["a"]


@pytest.mark.parametrize("raw, want", [("false", False), ('"false"', False), ('"0"', False),
                                       ("true", True), ('"yes"', True), (None, False)])
def test_auth_mode_parses_quoted_values(tmp_path, raw, want) -> None:
    body = "repos:\n  - {name: a, path: /a}\n" + (f"auth_mode: {raw}\n" if raw is not None else "")
    assert load_ecosystem_config(_cfg(tmp_path, body)).auth_mode is want


def test_unrecognised_flag_is_a_config_error(tmp_path) -> None:
    with pytest.raises(ValueError, match="auth_mode"):
        load_ecosystem_config(_cfg(tmp_path, "repos:\n  - {name: a, path: /a}\nauth_mode: maybe\n"))


def test_empty_repos_key_raises_the_intended_error(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least one enabled repository"):
        load_ecosystem_config(_cfg(tmp_path, "repos:\n"))
