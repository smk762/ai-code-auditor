"""Idempotent install of agent-pack awareness files into target repos."""
from __future__ import annotations

import textwrap

import pytest

from auditor import pack_awareness as pa


def test_creates_files_and_is_idempotent(tmp_path):
    first = pa.install_into_repo("argus-x", tmp_path)
    assert first.changed
    agents = (tmp_path / "AGENTS.md").read_text()
    rule = (tmp_path / ".cursor/rules/agent-coding-packs.mdc").read_text()
    assert "/audit/packs/bundle?repo=argus-x" in agents
    assert "ai-audit repair --pack-id" in agents
    assert rule.startswith("---\n") and "alwaysApply: true" in rule

    second = pa.install_into_repo("argus-x", tmp_path)
    assert not second.changed
    assert (tmp_path / "AGENTS.md").read_text() == agents
    assert agents.count(pa.MARKER_START) == 1


def test_appends_to_existing_agents_and_preserves_content(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Mine\n\nKeep me.\n")
    pa.install_into_repo("argus-x", tmp_path)
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.startswith("# Mine\n\nKeep me.\n")
    assert pa.MARKER_START in text

    # Content after the block survives a re-install; api_base change replaces in place.
    (tmp_path / "AGENTS.md").write_text(text + "\n## Trailing\nstay\n")
    pa.install_into_repo("argus-x", tmp_path, api_base="http://example:1")
    out = (tmp_path / "AGENTS.md").read_text()
    assert out.count(pa.MARKER_START) == 1
    assert "http://example:1/audit/packs/bundle?repo=argus-x" in out
    assert "192.168.1.198" not in out
    assert "## Trailing\nstay" in out and out.startswith("# Mine")


def test_user_authored_rule_not_clobbered_without_force(tmp_path):
    rule = tmp_path / ".cursor/rules/agent-coding-packs.mdc"
    rule.parent.mkdir(parents=True)
    rule.write_text("my own rule\n")
    res = pa.install_into_repo("argus-x", tmp_path)
    assert rule.read_text() == "my own rule\n"
    assert any("skipped" in a for a in res.actions)
    pa.install_into_repo("argus-x", tmp_path, force=True)
    assert pa.MARKER_START in rule.read_text()


def test_dry_run_writes_nothing(tmp_path):
    res = pa.install_into_repo("argus-x", tmp_path, dry_run=True)
    assert res.changed
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".cursor").exists()


def test_missing_path_is_skipped(tmp_path):
    res = pa.install_into_repo("argus-x", tmp_path / "nope")
    assert res.skipped


def test_install_all_filters_repo_and_skips_missing(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    cfg = tmp_path / "eco.yaml"
    cfg.write_text(textwrap.dedent(f"""
        repos:
          - {{name: repo-a, path: {a}, enabled: true}}
          - {{name: repo-b, path: {b}, enabled: true}}
          - {{name: repo-off, path: {a}, enabled: false}}
    """))
    all_res = pa.install_all(ecosystem_config=cfg)
    assert [r.repo for r in all_res] == ["repo-a", "repo-b"]
    assert all_res[1].skipped
    assert "repo=repo-a" in (a / "AGENTS.md").read_text()

    one = pa.install_all(ecosystem_config=cfg, repo="repo-a", dry_run=True)
    assert [r.repo for r in one] == ["repo-a"]
    with pytest.raises(ValueError):
        pa.install_all(ecosystem_config=cfg, repo="repo-off")
