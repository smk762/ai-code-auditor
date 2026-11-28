"""Idempotent install of agent-pack awareness files into target repos."""
from __future__ import annotations

import json
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
    assert "192.168." not in out  # no LAN address baked in
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


def _configs(tmp_path):
    """Two scanned repos with mirrors under tmp/mirrors, and a home where repo-a is checked out."""
    mirrors, home = tmp_path / "mirrors", tmp_path / "home"
    for d in (mirrors / "repo-a", mirrors / "repo-b", home / "repo-a" / ".git"):
        d.mkdir(parents=True)
    eco = tmp_path / "eco.yaml"
    eco.write_text(textwrap.dedent(f"""
        repos:
          - {{name: repo-a, path: {mirrors}/repo-a, enabled: true}}
          - {{name: repo-b, path: {mirrors}/repo-b, enabled: true}}
          - {{name: repo-off, path: {mirrors}/repo-a, enabled: false}}
    """))
    mir = tmp_path / "mirrors.yaml"
    mir.write_text(textwrap.dedent(f"""
        mirror_root: {mirrors}
        mirrors:
          - {{name: repo-a, url: https://x/repo-a.git, path: {mirrors}/repo-a}}
          - {{name: repo-b, url: https://x/repo-b.git, path: {mirrors}/repo-b}}
    """))
    return {"ecosystem_config": eco, "mirrors_config": mir}, mirrors, home


def test_install_all_targets_home_checkouts_filters_repo_and_skips_missing(tmp_path):
    cfg, mirrors, home = _configs(tmp_path)
    all_res = pa.install_all(home=home, **cfg)
    assert [r.repo for r in all_res] == ["repo-a", "repo-b"]
    assert all_res[1].skipped  # not checked out in home
    assert "repo=repo-a" in (home / "repo-a" / "AGENTS.md").read_text()
    assert not any((mirrors / "repo-a").iterdir())

    one = pa.install_all(home=home, repo="repo-a", dry_run=True, **cfg)
    assert [r.repo for r in one] == ["repo-a"]
    with pytest.raises(ValueError):
        pa.install_all(home=home, repo="repo-off", **cfg)


def test_a_scan_mirror_is_never_written(tmp_path):
    cfg, mirrors, home = _configs(tmp_path)
    (home / "repo-b").symlink_to(mirrors / "repo-b")  # like ~/argus-core -> mirrors/argus-cortex
    (mirrors / "other" / ".git").mkdir(parents=True)  # under mirror_root, though not configured
    res = pa.install_all(home=home, **cfg)
    assert res[1].skipped == "a scan mirror, never written"
    res = pa.install_all(paths=[mirrors / "repo-a", mirrors / "other", home / "repo-b"], **cfg)
    assert [r.skipped for r in res] == ["a scan mirror, never written"] * 3
    assert not list((mirrors / "repo-a").iterdir()) and not list((mirrors / "repo-b").iterdir())
    assert [p.name for p in (mirrors / "other").iterdir()] == [".git"]


def test_paths_install_into_named_checkouts_and_skip_non_checkouts(tmp_path):
    cfg, _, _ = _configs(tmp_path)
    work, plain = tmp_path / "work" / "my-app", tmp_path / "plain"
    (work / ".git").mkdir(parents=True)
    plain.mkdir()
    res = pa.install_all(paths=[work, plain], **cfg)
    assert [(r.repo, r.skipped) for r in res] == [("my-app", ""), ("plain", "not a git checkout")]
    assert (work / "AGENTS.md").exists() and not (plain / "AGENTS.md").exists()


def test_hook_only_writes_only_the_hook(tmp_path):
    cfg, _, home = _configs(tmp_path)
    res = pa.install_all(home=home, repo="repo-a", hook_only=True, **cfg)
    root = home / "repo-a"
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) == \
        [".claude/settings.local.json", ".git/info/exclude"]
    assert all("AGENTS.md" not in a for a in res[0].actions)
    hook = json.loads((root / pa.CLAUDE_SETTINGS_RELPATH).read_text())["hooks"]["PostToolUse"][0]
    assert pa.HOOK_MODULE in hook["hooks"][0]["command"]
