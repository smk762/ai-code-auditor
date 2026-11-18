"""run_merge_refresh: event refs must be SHAs; a missing ``before`` is the parent of ``after``."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auditor import merge_hook
from auditor.merge_hook import MergeEvent, run_merge_refresh


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i in range(3):
        (repo / "f.py").write_text(f"x = {i}\n", encoding="utf-8")
        _git(repo, "add", "f.py")
        _git(repo, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(repo, "rev-parse", "HEAD"))

    import auditor.agent_pack as ap
    import auditor.config as config
    import auditor.diff_auditor as da
    import auditor.repo_resolver as rr
    import conductor.mirrors as cm

    repo_cfg = SimpleNamespace(name="r")
    monkeypatch.setattr(config, "load_ecosystem_config",
                        lambda: SimpleNamespace(repos=[repo_cfg], output_dir=str(tmp_path / "out")))
    monkeypatch.setattr(rr, "resolve_repo_path", lambda _cfg: repo)
    monkeypatch.setattr(ap, "pack_rag_ingest_enabled", lambda _x=None: False)
    monkeypatch.setattr(ap, "emit_world_model_packs", lambda **kw: [])
    diffs: list[tuple[str, str]] = []
    monkeypatch.setattr(da, "get_diff_between_refs", lambda _p, b, a="HEAD": diffs.append((b, a)) or "")
    synced: list[str] = []
    monkeypatch.setattr(cm, "sync_one_mirror", lambda name, **kw: synced.append(name))
    return {"shas": shas, "diffs": diffs, "synced": synced}


def test_missing_before_uses_the_parent_of_after_not_head(env: dict[str, Any]) -> None:
    c0, c1, _c2 = env["shas"]  # HEAD is c2; the push being audited is c1
    out = run_merge_refresh(MergeEvent(repo="r", before=merge_hook._ZERO_SHA, after=c1), sync_mirror=False)
    assert env["diffs"] == [(c0, c1)]
    assert out["before"] == c0


@pytest.mark.parametrize("field", ["before", "after"])
def test_non_sha_refs_are_refused_before_any_git(env: dict[str, Any], field: str) -> None:
    ev = MergeEvent(repo="r", **{field: "--output=/tmp/pwned"})
    with pytest.raises(ValueError, match="not a commit SHA"):
        run_merge_refresh(ev, sync_mirror=True)
    assert env["synced"] == [] and env["diffs"] == []
