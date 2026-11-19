"""Regression tests for wiki/build.py audit packs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wiki import build as build_mod


def test_top_index_survives_a_write_that_dies_halfway(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "wiki"
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "INDEX.json").write_text(json.dumps({"repo": "svc", "pages": [{"slug": "overview"}]}))
    build_mod._write_top_index(tmp_path)
    before = (root / "INDEX.json").read_text()

    real = Path.write_text

    def dies_halfway(self, data, *a, **kw):  # a crash / full disk mid-write
        real(self, data[: len(data) // 2], *a, **kw)
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", dies_halfway)
    with pytest.raises(OSError):
        build_mod._write_top_index(tmp_path)
    monkeypatch.undo()

    assert (root / "INDEX.json").read_text() == before  # readers still see the whole old index
    assert json.loads(before)["repos"]["svc"]["pages"] == 1
    assert sorted(p.name for p in root.iterdir()) == ["INDEX.json", "svc"]  # no temp file left behind


def test_top_index_skips_malformed_repo_indexes(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    for name, body in {"good": {"repo": "good", "pages": [{"slug": "overview"}]},
                       "norepo": {"pages": []}, "alist": [1, 2], "scalar": 7}.items():
        (root / name).mkdir(parents=True)
        (root / name / "INDEX.json").write_text(json.dumps(body))
    build_mod._write_top_index(tmp_path)
    assert json.loads((root / "INDEX.json").read_text())["repos"] == {
        "good": {"built_at": None, "source_sha": None, "branch": None, "pages": 1}}


@pytest.mark.parametrize("previous", [
    [{"slug": "overview"}],                                  # top level is a list
    {"pages": [{"file": "overview.md"}]},                    # entry without slug
    {"pages": ["overview"]},                                 # non-dict entry
    {"pages": [{"slug": "overview"}]},                       # entry without file
])
def test_write_repo_ignores_a_malformed_previous_index(tmp_path: Path, previous) -> None:
    from types import SimpleNamespace

    d = tmp_path / "wiki" / "_public" / "svc"
    d.mkdir(parents=True)
    (d / "INDEX.json").write_text(json.dumps(previous))
    (d / "overview.md").write_text("# old\n")
    report = build_mod.BuildReport(repos={"svc": build_mod.RepoResult("svc")})
    c = {"repo": SimpleNamespace(path=str(tmp_path)), "root": tmp_path, "verify": {}, "requested": set()}
    assert build_mod._write_repo("svc", c, {}, report, tmp_path, public=True) == []
    idx = json.loads((d / "INDEX.json").read_text())
    assert idx["repo"] == "svc" and idx["pages"] == []


def test_public_build_does_not_scan_notes_for_skipped_repos(tmp_path: Path, monkeypatch) -> None:
    from auditor.config import RepoConfig
    from wiki import notes as notes_mod
    from wiki import public

    class _Cfg:
        repos = [RepoConfig("argus-good", str(tmp_path)), RepoConfig("argus-private", str(tmp_path)),
                 RepoConfig("voluptas", str(tmp_path))]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.setattr(public, "public_inputs", lambda *a, **k: (
        lambda s: s, "", {"argus-good": "public", "argus-private": "private", "voluptas": "public"}))
    scanned: list[str] = []

    def scan(repo, out):
        scanned.append(repo.name)
        raise RuntimeError("mirror missing")

    monkeypatch.setattr(notes_mod, "scan_notes", scan)
    rep = build_mod.build(None, tmp_path / "reports", dry_run=True, public=True, runner=object())
    assert scanned == ["argus-good"]
    assert rep.repos["argus-private"].skipped == "not a public argus repo"
    assert rep.repos["voluptas"].skipped == "not a public argus repo"
