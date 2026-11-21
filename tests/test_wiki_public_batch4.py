"""Opsec gate and export/publish robustness (batch-4 packs). The gate still fails closed and refuses."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from auditor.config import RepoConfig
from tests.test_wiki_public import VIS, _bare, wiki  # noqa: F401  (fixture)
from wiki import public


def test_private_names_with_capitals_still_drop_their_rows() -> None:
    doc = public.public_ecosystem_doc("| Area | Repo |\n| Agents | [mithrandir](x) |\n| Good | [argus-good](y) |\n",
                                      ["Mithrandir"])
    assert "mithrandir" not in doc.lower() and "argus-good" in doc


@pytest.mark.parametrize("text", [
    "## Deployment notes\n\nruns on Jarrah\n",                     # at the very start
    "# Eco\n\nok\n\n## Deployment Notes\n\nruns on Jarrah\n",       # other case
    "# Eco\n\nok\n\n##  deployment notes (current)\n\nruns on Jarrah\n",  # other spacing
    "# Eco\n\nok\n\n## Night Conductor\n\nruns on Jarrah\n",
])
def test_ops_sections_are_cut_whatever_their_case_or_place(text: str) -> None:
    assert "Jarrah" not in public.public_ecosystem_doc(text, [])


def test_list_items_naming_a_private_repo_are_dropped() -> None:
    doc = public.public_ecosystem_doc(
        "# Eco\n\n- **argus-mint** (minting)\n* **voluptas** (ComfyUI)\n1. mithrandir agents\n- **argus-good** (ok)\n",
        ["argus-mint", "voluptas", "mithrandir"])
    for leak in ("argus-mint", "voluptas", "mithrandir"):
        assert leak not in doc
    assert "argus-good" in doc


def test_a_disabled_private_repo_is_still_refused(wiki: Path, monkeypatch) -> None:  # noqa: F811
    (wiki / "wiki" / "argus-good" / "interfaces.md").write_text("# Interfaces\n\nTalks to argus-secret.\n")
    asked: list[str] = []

    class _Cfg:
        repos = [RepoConfig("argus-good", "/x"), RepoConfig("argus-secret", "/x", enabled=False)]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    vis = {"argus-good": "public", "argus-secret": "private"}

    def lookup(r: str) -> str | None:
        asked.append(r)
        return vis.get(r)

    rep = public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=lookup)
    assert "argus-secret" in asked
    assert rep.exported == {} and [(r.repo, r.page, r.match) for r in rep.refused] == [
        ("argus-good", "interfaces", "argus-secret")]
    assert "argus-secret" not in rep.skipped  # disabled: gated against, never a candidate for export


def test_public_inputs_know_disabled_private_repos(tmp_path: Path, monkeypatch) -> None:
    from wiki import pages as pages_mod

    class _Cfg:
        repos = [RepoConfig("argus-good", "/x"), RepoConfig("argus-secret", "/x", enabled=False)]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    eco = tmp_path / "eco.md"
    eco.write_text("# Eco\n\nargus-good calls argus-secret.\n")
    monkeypatch.setattr(pages_mod, "ECOSYSTEM_DOC", eco)
    (tmp_path / "deny.txt").write_text("Jarrah\n")
    scrub, doc, vis = public.public_inputs(tmp_path / "deny.txt",
                                           {"argus-good": "public", "argus-secret": "private"}.get)
    assert vis["argus-secret"] == "private" and "argus-secret" not in doc and "argus-secret" not in scrub("x argus-secret")


def test_publish_gates_against_disabled_private_repos(wiki: Path, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    from wiki import publish as pub

    deny = wiki.parent / "deny.txt"
    public.export(wiki, denylist_path=deny, visibility=VIS.get)
    b = wiki / "wiki_public"
    (b / "argus-good" / "overview.html").write_text("<p>uses argus-secret</p>")
    m = json.loads((b / "manifest.json").read_text())
    m["bundle_sha256"] = public.bundle_hash_of(b)
    (b / "manifest.json").write_text(json.dumps(m))

    class _Cfg:
        repos = [RepoConfig("argus-good", "/x"), RepoConfig("argus-secret", "/x", enabled=False)]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    with pytest.raises(RuntimeError, match="opsec gate failed"):
        pub.publish(b, repo=_bare(tmp_path), checkout=tmp_path / "co", denylist_path=deny,
                    visibility={"argus-good": "public", "argus-secret": "private"}.get)


def test_a_missing_page_file_skips_that_repo_only(wiki: Path, monkeypatch) -> None:  # noqa: F811
    (wiki / "wiki" / "argus-leaky" / "integration.md").unlink()  # still listed in its INDEX.json
    rep = public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=VIS.get)
    assert rep.exported == {"argus-good": ["overview", "interfaces"]}
    assert rep.skipped["argus-leaky"].startswith("a listed page could not be read")
    assert not (wiki / "wiki_public" / "argus-leaky").exists()


def test_publish_refuses_a_checkout_of_another_repo(wiki: Path, tmp_path: Path) -> None:  # noqa: F811
    from wiki import publish as pub

    deny = wiki.parent / "deny.txt"
    public.export(wiki, denylist_path=deny, visibility=VIS.get)
    old_remote = _bare(tmp_path)
    co = tmp_path / "co"
    first = pub.publish(wiki / "wiki_public", repo=old_remote, checkout=co, push=True, denylist_path=deny,
                        visibility=VIS.get)
    other = tmp_path / "other"
    other.mkdir()
    new_remote = _bare(other)
    with pytest.raises(RuntimeError, match="refusing to publish to the wrong repo"):
        pub.publish(wiki / "wiki_public", repo=new_remote, checkout=co, push=True, denylist_path=deny,
                    visibility=VIS.get)
    assert not subprocess.run(["git", "-C", new_remote, "rev-parse", "--verify", "-q", "main"],
                              capture_output=True, text=True).stdout.strip()
    # the same repo, named by an equivalent path, is still fine
    again = pub.publish(wiki / "wiki_public", repo=str(Path(old_remote).parent / "." / "argus-docs.git"),
                        checkout=co, push=True, denylist_path=deny, visibility=VIS.get)
    assert again["commit"] == first["commit"]
