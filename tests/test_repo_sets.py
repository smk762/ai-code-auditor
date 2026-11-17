"""The scan set (config/ecosystem.yaml) and the sync set (config/repo_mirrors.yaml) list the same repos.

A repo in only one of them is either scanned from a mirror nothing syncs, or synced and never scanned
(which is how repos went missing from the findings page)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _names(path: str, key: str) -> dict[str, str]:
    data = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return {r["name"]: r["path"] for r in data[key]}


def test_scan_set_and_mirror_set_match() -> None:
    scan = _names("config/ecosystem.yaml", "repos")
    mirrors = _names("config/repo_mirrors.yaml", "mirrors")
    assert scan == mirrors  # same names and the same paths


def test_this_repo_is_scanned_from_a_mirror_not_the_working_checkout() -> None:
    assert _names("config/ecosystem.yaml", "repos")["ai-code-auditor"] == "/home/smk/mirrors/ai-code-auditor"


def test_generated_docs_bundle_is_not_scanned() -> None:
    assert "argus-docs" not in _names("config/ecosystem.yaml", "repos")
