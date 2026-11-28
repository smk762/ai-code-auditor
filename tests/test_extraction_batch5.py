"""Extraction: repo-relative paths, a race with the nightly sync, copy-risk git parsing, id memory,
the pack's location and score (auditor/extraction.py)."""
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from auditor import agent_pack
from auditor import extraction as ex
from auditor.agent_pack import load_pack


@pytest.fixture(autouse=True)
def _no_rag(monkeypatch):
    monkeypatch.setenv("AI_AUDIT_PACK_RAG_INGEST", "0")


def _cand(cid: str, units: list[tuple[str, str, str]]) -> dict:
    return {"id": cid, "title": f"Extract {units[0][2]}", "extraction_score": 0.9,
            "repos_involved": sorted({r for r, _, _ in units}),
            "source_units": [{"repo": r, "path": f"/home/smk/mirrors/{r}/{p}", "symbols": [s]} for r, p, s in units]}


SHA = [("argus-atlas", "src/argus_atlas/hashing.py", "sha256_bytes"),
       ("argus-mint", "src/argus_mint/cards.py", "sha256_hex"),
       ("argus-curator", "src/argus_curator/consent.py", "_sha256"),
       ("argus-echo", "src/echo/util.py", "_sha")]
NOW = [("agent-composer", "app/util.py", "_now"), ("argus-forge", "src/f/t.py", "_now")]


@pytest.fixture
def world(tmp_path: Path) -> dict:
    out, mirrors = tmp_path / "reports", tmp_path / "mirrors"
    out.mkdir()
    for repo, path, sym in SHA + NOW:
        f = mirrors / repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"def {sym}(b):\n    return b\n")
    (out / "extraction_candidates.yaml").write_text(yaml.safe_dump({"candidates": [_cand("ext-a", SHA)]}))
    paths = {r: str(mirrors / r) for r, _, _ in SHA + NOW}
    return {"out": out, "paths": paths, "decisions": tmp_path / "rules" / "d.json"}


def test_a_package_named_after_the_repo_keeps_its_directory() -> None:
    assert ex.rel_path("voluptas", "/home/smk/mirrors/voluptas/voluptas/nodes.py") == "voluptas/nodes.py"
    assert ex.rel_path("voluptas", "/home/smk/mirrors/voluptas/__init__.py") == "__init__.py"
    assert ex.rel_path("r", "C:\\m\\r\\r\\a.py") == "r/a.py"


def test_an_approval_landing_while_the_sync_waits_for_the_lock_is_not_closed(world: dict, monkeypatch) -> None:
    real = agent_pack.pack_lock
    raced = {"done": False}

    @contextmanager
    def racing(output_dir):
        if not raced["done"]:  # the approval wins the lock just before the sync gets it
            raced["done"] = True
            ex.decide(world["out"], "ext-a", "approved", repo_paths=world["paths"], path=world["decisions"])
        with real(output_dir):
            yield

    monkeypatch.setattr(agent_pack, "pack_lock", racing)
    res = ex.sync_packs(world["out"], world["paths"], path=world["decisions"])
    assert res["closed"] == 0 and res["refreshed"] == 1
    assert load_pack(world["out"], "pack-ext-a")["status"] == "open"


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def _repo(root: Path, files: list[str]) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")
    return _git(root, "rev-parse", "HEAD").strip()


def test_paths_starting_with_at_or_holding_non_ascii_are_attributed(tmp_path: Path) -> None:
    sha = _repo(tmp_path / "web", ["@scope/a.js", "déjà.py", "plain.py"])
    out = ex._agent_touches({"web": tmp_path / "web"}, [{"url": "pr-1", "commit_shas": [sha]}])
    assert {p for (_, p) in out} == {"@scope/a.js", "déjà.py", "plain.py"}
    assert all(v == {"pr-1"} for v in out.values())


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(["git"], 120), FileNotFoundError("git")],
                         ids=["timeout", "no_git"])
def test_one_mirror_git_cannot_read_is_skipped_not_fatal(tmp_path: Path, monkeypatch, exc) -> None:
    good = _repo(tmp_path / "good", ["a.py"])
    _repo(tmp_path / "slow", ["b.py"])
    real = subprocess.run

    def run(cmd, *a, **kw):
        if str(tmp_path / "slow") in [str(c) for c in cmd]:
            raise exc
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    out = ex._agent_touches({"slow": tmp_path / "slow", "good": tmp_path / "good"},
                            [{"url": "pr-1", "commit_shas": [good]}])
    assert out == {("good", "a.py"): {"pr-1"}}


def test_an_id_missing_from_one_run_comes_back_with_its_units(tmp_path: Path) -> None:
    first = [_cand("x", SHA), _cand("y", NOW)]
    ex.stabilize_ids(first, tmp_path)
    sha_id = first[0]["id"]
    ex.stabilize_ids([_cand("y", NOW)], tmp_path)  # a partial run: the SHA cluster wasn't seen
    back = [_cand("x", SHA[:3])]  # back, one copy migrated away
    ex.stabilize_ids(back, tmp_path)
    assert back[0]["id"] == sha_id


def test_the_pack_location_never_names_a_consumer_file_under_the_target_repo(world: dict) -> None:
    ex.decide(world["out"], "ext-a", "approved", repo_paths=world["paths"], path=world["decisions"])
    pack = load_pack(world["out"], "pack-ext-a")
    assert pack["repo"]["name"] == "argus-cortex"
    assert pack["location"]["file_path"] == ""


def test_a_score_of_zero_stays_zero() -> None:
    copies = [{"repo": "argus-mint", "path": "a.py", "symbol": "f", "state": "left"}]
    pack = ex.build_pack("ext-z", {"score": 0, "symbol": "f"}, copies)
    assert pack.priority["score"] == 0.0
    assert ex.build_pack("ext-z", {"symbol": "f"}, copies).priority["score"] == 0.6
