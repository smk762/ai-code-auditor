"""merge_hook: a mirror that already had the merge is not read as an empty diff."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_a_pre_sync_tip_equal_to_after_falls_back_to_the_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import auditor.agent_pack as ap
    import auditor.config as config
    import auditor.diff_auditor as da
    import auditor.repo_resolver as rr
    import conductor.mirrors as mirrors
    from auditor.merge_hook import MergeEvent, run_merge_refresh

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i in range(2):
        (repo / "f.py").write_text(f"x = {i}\n", encoding="utf-8")
        _git(repo, "add", "f.py")
        _git(repo, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(repo, "rev-parse", "HEAD"))

    monkeypatch.setattr(config, "load_ecosystem_config",
                        lambda: SimpleNamespace(repos=[SimpleNamespace(name="r")], output_dir=str(tmp_path / "out")))
    monkeypatch.setattr(rr, "resolve_repo_path", lambda _cfg: repo)
    monkeypatch.setattr(ap, "pack_rag_ingest_enabled", lambda _x=None: False)
    monkeypatch.setattr(ap, "emit_world_model_packs", lambda **kw: [])
    # the mirror is already at the merge: syncing is a no-op
    monkeypatch.setattr(mirrors, "sync_one_mirror", lambda name, branch=None: SimpleNamespace(
        ok=True, action="up_to_date", branch="dev", path=str(repo), error=""))
    ranges: list[tuple[str, str]] = []

    def fake_diff(_p, b, a="HEAD"):
        ranges.append((b, a))
        return ""

    monkeypatch.setattr(da, "get_diff_between_refs", fake_diff)
    out: dict[str, Any] = run_merge_refresh(MergeEvent(repo="r", before="", after=shas[1]), sync_mirror=True)
    assert ranges == [(shas[0], shas[1])]
    assert out["ok"] is True
