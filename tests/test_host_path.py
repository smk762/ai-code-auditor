"""``~`` in configured paths is the host user's home, inside Docker too."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auditor import git_ops
from auditor.config import load_ecosystem_config
from auditor.fsutil import host_path
from conductor.mirrors import load_mirrors_config

ROOT = Path(__file__).resolve().parents[1]


def test_host_home_wins_over_the_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/")
    monkeypatch.setenv("AI_AUDIT_HOST_HOME", "/home/host")
    assert host_path("~/mirrors/x") == "/home/host/mirrors/x"
    for same in ("/srv/x", "./x", "x/~/y", "https://example.invalid/x.git", "~other/x", ""):
        assert host_path(same) == same


def test_no_usable_home_is_an_error_not_a_root_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/")
    monkeypatch.delenv("AI_AUDIT_HOST_HOME", raising=False)
    with pytest.raises(ValueError, match="AI_AUDIT_HOST_HOME"):
        host_path("~/mirrors/x")


def test_every_loader_expands_the_checked_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_HOST_HOME", "/home/host")
    repos = {r.name: r.path for r in load_ecosystem_config(ROOT / "config/ecosystem.yaml").repos}
    cfg = load_mirrors_config(ROOT / "config/repo_mirrors.yaml")
    assert repos == {m["name"]: m["path"] for m in cfg["mirrors"]}
    assert all(p.startswith("/home/host/") for p in [*repos.values(), cfg["mirror_root"]])
    assert Path(repos["ai-code-auditor"]) in git_ops.scan_source_paths()


def test_compose_passes_the_hosts_home_to_every_service() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    assert all("AI_AUDIT_HOST_HOME=${HOME}" in s["environment"] for s in services.values())


def test_no_home_directory_is_checked_in() -> None:
    shipped = [ROOT / "docker-compose.yml", ROOT / "CLAUDE.md", ROOT / "AGENTS.md",
               *(p for d in ("auditor", "conductor", "config", "contrib", "scripts")
                 for p in (ROOT / d).rglob("*") if p.suffix in {".py", ".yaml", ".sh", ".md", ".service", ".timer"})]
    assert not [str(p) for p in shipped if "/home/smk" in p.read_text(encoding="utf-8")]
