"""wiki/public.py export: a page without a title, and a build that fails part way (batch-5 packs).
The opsec gate still fails closed and refuses; no network (visibility is a dict lookup)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import tests.test_wiki_public as _shared
from wiki import public

VIS = _shared.VIS
wiki = _shared.wiki  # the shared fixture, bound as an assignment so test params named after it lint clean


def _drop_title(wiki: Path, repo: str, slug: str, value=None) -> None:
    idx_path = wiki / "wiki" / repo / "INDEX.json"
    idx = json.loads(idx_path.read_text())
    for p in idx["pages"]:
        if p["slug"] == slug:
            if value is None:
                p.pop("title")
            else:
                p["title"] = value
    idx_path.write_text(json.dumps(idx))


@pytest.mark.parametrize("value", [None, "", 7], ids=["absent", "empty", "not_a_string"])
def test_a_page_without_a_title_leaves_its_repo_out_not_the_export(wiki: Path, value) -> None:
    _drop_title(wiki, "argus-good", "interfaces", value)
    rep = public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=VIS.get)  # was KeyError
    assert "argus-good" not in rep.exported
    assert "no title" in rep.skipped["argus-good"]
    assert not (wiki / "wiki_public" / "argus-good").exists()


def test_a_title_that_fails_the_gate_refuses_the_page(wiki: Path) -> None:
    _drop_title(wiki, "argus-good", "interfaces", "Interfaces on Jarrah")
    rep = public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=VIS.get)
    assert "argus-good" not in rep.exported
    assert any(r.repo == "argus-good" and r.page == "interfaces" for r in rep.refused)
    assert "Jarrah" not in (wiki / "wiki_public" / "manifest.json").read_text()


def _staging(wiki: Path) -> list[str]:
    return sorted(p.name for p in wiki.iterdir() if ".staging-" in p.name)


def test_a_build_that_fails_part_way_leaves_no_staging_dir(wiki: Path, monkeypatch) -> None:
    def broken(hashes):
        raise OSError("disk full")

    monkeypatch.setattr(public, "bundle_sha256", broken)
    with pytest.raises(OSError, match="disk full"):
        public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=VIS.get)
    assert _staging(wiki) == []  # was [".wiki_public.staging-<pid>"], one more every failed night


def test_a_staging_dir_from_a_dead_export_is_cleared_and_a_live_ones_kept(wiki: Path) -> None:
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (wiki / f".wiki_public.staging-{dead}").mkdir()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (wiki / f".wiki_public.staging-{live.pid}").mkdir()
        public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=VIS.get)
        assert _staging(wiki) == [f".wiki_public.staging-{live.pid}"]
    finally:
        live.kill()
        live.wait()
