"""Every route that changes state or starts work requires the API token (CLAUDE.md "Must never happen").

Sweeps the app's routes, so a new endpoint without ``_require_mutating_auth`` fails here instead of
relying on someone writing a per-endpoint test."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from auditor import audit_api
from auditor.diff_auditor import git_ref_arg

# Routes that authenticate another way. Each needs its reason.
OWN_AUTH = {
    ("POST", "/audit/hooks/github"): "GitHub HMAC signature, else the same token check inline",
}


def _calls(dependant) -> set:
    found = {dependant.call}
    for d in dependant.dependencies:
        found |= _calls(d)
    return found


def _non_get_routes():
    for r in audit_api.app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods - {"GET", "HEAD", "OPTIONS"}):
                yield m, r


def test_every_non_get_route_requires_the_token() -> None:
    missing = [f"{m} {r.path}" for m, r in _non_get_routes()
               if (m, r.path) not in OWN_AUTH and audit_api._require_mutating_auth not in _calls(r.dependant)]
    assert missing == []


def test_own_auth_exemptions_still_exist() -> None:
    present = {(m, r.path) for m, r in _non_get_routes()}
    assert set(OWN_AUTH) <= present


@pytest.mark.parametrize("path", ["/audit/validate", "/audit/diff", "/audit/git/diff"])
def test_previously_open_routes_refuse_without_the_token(path: str, monkeypatch) -> None:
    monkeypatch.setenv("AI_AUDIT_API_TOKEN", "tok")
    assert TestClient(audit_api.app).post(path, json={"repo": "x", "compare_branch": "dev"}).status_code == 401


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "-p", "dev main", "a..b", "", "x\n"])
def test_refs_that_could_become_git_options_are_refused(ref: str) -> None:
    with pytest.raises(RuntimeError):
        git_ref_arg(ref)


@pytest.mark.parametrize("ref", ["dev", "origin/dev", "feature/x-1", "HEAD~2", "a1b2c3d", "v1.2.0"])
def test_ordinary_refs_pass(ref: str) -> None:
    assert git_ref_arg(ref) == ref
