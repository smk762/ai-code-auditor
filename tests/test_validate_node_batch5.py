"""Regression tests for auditor/validate_node.py audit packs (batch 5)."""
from __future__ import annotations

import json

import pytest

from auditor import validate_node

_OK = {"version": "1.0.0", "resolved": "https://registry.npmjs.org/x/-/x-1.0.0.tgz"}


def _refusal(key: str) -> str:
    lock = {"name": "web", "lockfileVersion": 3, "packages": {"": {"name": "web"}, key: _OK}}
    return validate_node.install_refusal(json.dumps({"name": "web"}).encode(), json.dumps(lock).encode())


@pytest.mark.parametrize("key", [
    "node_modules/../../outside", "../sibling/node_modules/x", "/abs/node_modules/x",
    "node_modules/x/node_modules/../../../y", "node_modules/@s/..", "node_modules/..\\..\\x",
    "node_modules/.", "node_modules", "node_modules/", "node_modules/@scope", "web/node_modules/x",
], ids=[f"key{i}" for i in range(11)])
def test_a_lockfile_key_outside_node_modules_is_refused(monkeypatch, key) -> None:  # c39d4bca85
    monkeypatch.delenv("AI_AUDIT_NPM_REGISTRIES", raising=False)
    monkeypatch.delenv("npm_config_registry", raising=False)
    assert "local path" in _refusal(key)


@pytest.mark.parametrize("key", ["node_modules/x", "node_modules/@s/y", "node_modules/x/node_modules/@s/y",
                                 "node_modules/a/node_modules/b"], ids=["plain", "scoped", "nested_scoped", "nested"])
def test_ordinary_lockfile_keys_pass(monkeypatch, key) -> None:
    monkeypatch.delenv("AI_AUDIT_NPM_REGISTRIES", raising=False)
    monkeypatch.delenv("npm_config_registry", raising=False)
    assert _refusal(key) == ""
