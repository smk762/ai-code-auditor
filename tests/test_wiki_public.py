from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor.config import RepoConfig
from wiki import public


DENY = ["Jarrah", "holly"]


@pytest.mark.parametrize("text,reason", [
    ("db at 192.168.1.109:5432", "private or link-local IP"),
    ("Postgres lives at 192.168.1.109.", "private or link-local IP"),  # end of a sentence
    ("talks to 10.0.0.4", "private or link-local IP"),
    ("link-local 169.254.1.1", "private or link-local IP"),
    ("ipv6 fe80::1ff:fe23", "private or link-local IP"),
    ("cloned under /home/alice/mirrors/x", "home or mount path"),
    ("data in /mnt/nas", "home or mount path"),
    ("mail ops@company.com", "email address"),
    ("reach nas.lan or box.local", "LAN hostname"),
    ("renders through voluptas", "private repo named"),
    ("runs on JARRAH overnight", "deny-listed term"),
    ("persona alias holly", "deny-listed term"),
    ("reads VOLUPTAS_URL from the env", "private repo named"),  # inside an identifier
    ("the argus-voluptas bridge", "private repo named"),
])
def test_gate_refuses(text: str, reason: str) -> None:
    assert public.gate(text, DENY, ["voluptas"])[0] == reason


@pytest.mark.parametrize("text", [
    "binds 127.0.0.1:8188 and 0.0.0.0", "public 8.8.8.8", "see https://github.com/o/r/blob/x/a.py#L3",
    "user@example.com", "bot@users.noreply.github.com", "the dragonhound site",
    "version 1.2.3.4 of the schema", "release 10.0.0.4.1 notes", "a hollyhock (longer word: not the term)",
])
def test_gate_allows(text: str) -> None:
    assert public.gate(text, DENY, ["voluptas"]) is None


def test_denylist_is_required(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_AUDIT_OPSEC_DENYLIST", raising=False)
    with pytest.raises(FileNotFoundError):
        public.load_denylist(tmp_path / "missing.txt")
    f = tmp_path / "deny.txt"
    f.write_text("# comment\nJarrah  # machine\n\nholly\n")
    assert public.load_denylist(f) == ["Jarrah", "holly"]


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch) -> Path:
    out = tmp_path / "reports"

    def page(repo: str, slug: str, md: str, title: str = "") -> dict:
        d = out / "wiki" / repo
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.md").write_text(md)
        return {"slug": slug, "title": title or slug.title(), "file": f"{slug}.md", "source_sha": "abc123"}

    def index(repo: str, pages: list[dict]) -> None:
        (out / "wiki" / repo / "INDEX.json").write_text(json.dumps(
            {"repo": repo, "source_sha": "abc123", "branch": "dev", "built_at": "t", "pages": pages}))

    index("argus-good", [page("argus-good", "overview", "# Good\n\nBuilt by `make` [src/a.py:L1-L2].\n\n```mermaid\ngraph TD; A-->B\n```\n"),
                         page("argus-good", "interfaces", "# Interfaces\n\nCLI.\n"),
                         page("argus-good", "findings", "# Notable findings\n\nsecret stuff\n")])
    index("argus-leaky", [page("argus-leaky", "overview", "# Fine\n"),
                          page("argus-leaky", "integration", "# Integration\n\nPostgres at 192.168.1.109.\n")])
    index("argus-private", [page("argus-private", "overview", "# Hidden\n")])
    index("agent-composer", [page("agent-composer", "overview", "# Not argus\n")])

    pp = tmp_path / "m" / "argus-good"
    pp.mkdir(parents=True)
    (pp / "pyproject.toml").write_text('[project]\nname = "argus-good"\n')

    class _Cfg:
        repos = [RepoConfig("argus-good", str(pp)), RepoConfig("argus-leaky", "/x"), RepoConfig("argus-private", "/x"),
                 RepoConfig("argus-unbuilt", "/x"), RepoConfig("agent-composer", "/x"), RepoConfig("voluptas", "/x")]

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    (tmp_path / "deny.txt").write_text("Jarrah\n")
    return out


VIS = {"argus-good": "public", "argus-leaky": "public", "argus-private": "private", "argus-unbuilt": "public",
       "agent-composer": "public", "voluptas": "private"}


def test_export_bundle(wiki: Path) -> None:
    rep = public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=VIS.get)
    b = wiki / "wiki_public"
    assert rep.exported == {"argus-good": ["overview", "interfaces"]}  # never "findings"
    assert rep.skipped["argus-leaky"].startswith("a page was refused")  # left out whole, incl. its clean page
    assert rep.skipped["argus-private"] == "GitHub visibility: private"
    assert rep.skipped["argus-unbuilt"] == "no wiki built" and rep.skipped["agent-composer"].startswith("not an argus")
    assert [(r.repo, r.page, r.reason) for r in rep.refused] == [("argus-leaky", "integration", "private or link-local IP")]
    assert sorted(p.name for p in b.iterdir()) == ["README.md", "argus-good", "manifest.json"]
    assert sorted(f.name for f in (b / "argus-good").iterdir()) == ["interfaces.html", "overview.html"]  # built output only
    html = (b / "argus-good" / "overview.html").read_text()
    assert "mermaid" not in html
    assert 'href="https://github.com/smk762/argus-good/blob/abc123/src/a.py#L1-L2"' in html
    m = json.loads((b / "manifest.json").read_text())["repos"]["argus-good"]
    assert m["sha"] == "abc123" and m["pypi"] == "argus-good" and len(m["pages"][0]["sha256"]) == 64
    manifest = json.loads((b / "manifest.json").read_text())
    assert manifest["bundle_sha256"] == public.bundle_hash_of(b) == rep.bundle_sha256
    report = json.loads((wiki / "wiki_public_report.json").read_text())  # outside the bundle
    assert report["refused"][0]["match"] == "192.168.1.109"
    assert "192.168" not in "".join(f.read_text() for f in b.rglob("*") if f.is_file())


def test_export_rewrites_bundle_and_treats_failed_lookups_as_private(wiki: Path) -> None:
    public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=VIS.get)
    rep = public.export(wiki, denylist_path=wiki.parent / "deny.txt", visibility=lambda r: None)
    assert rep.exported == {} and rep.skipped["argus-good"] == "GitHub visibility: lookup failed"
    assert not (wiki / "wiki_public" / "argus-good").exists()  # a repo that's no longer public disappears


def test_export_refuses_to_run_without_denylist(wiki: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_AUDIT_OPSEC_DENYLIST", raising=False)
    with pytest.raises(FileNotFoundError):
        public.export(wiki, denylist_path=wiki.parent / "nope.txt", visibility=VIS.get)
    assert not (wiki / "wiki_public").exists()


# ── public variant: scrubbed inputs ("placeholders at the source") ───────────

ECO = """| Area | Repo | Role |
|------|------|------|
| Captioning | [argus-good](https://github.com/o/argus-good) | Captions |
| ComfyUI | [voluptas](https://github.com/o/voluptas) | ComfyUI node |

**Not scanned (integration only):** mithrandir receives status.

## Ecosystem flow

```
  good ──► voluptas (ComfyUI :8188)
```

## Deployment notes (current)

| Jarrah `<STATEFUL_HOST>` | Postgres |
"""


def test_public_ecosystem_doc_and_scrubber() -> None:
    doc = public.public_ecosystem_doc(ECO, ["voluptas", "mithrandir"])
    assert "Deployment notes" not in doc and "Jarrah" not in doc and "Not scanned" not in doc
    assert "[voluptas]" not in doc and "argus-good" in doc  # private table row dropped
    scrub = public.scrubber_for(["Jarrah", "holly"], ["voluptas", "mithrandir"], ECO)
    out = scrub(doc + "\nuses VOLUPTAS and mithrandir on jarrah via holly, at 192.168.1.9:9000 or /home/u/x or nas.lan.")
    assert "an external ComfyUI service (ComfyUI :8188)" in out and "an external service" in out
    for leak in ("voluptas", "mithrandir", "jarrah", "holly", "192.168", "/home/", "nas.lan"):
        assert leak not in out.lower()
    assert public.gate(out, ["Jarrah", "holly"], ["voluptas", "mithrandir"]) is None


def test_public_build_scrubs_every_model_input(tmp_path: Path, monkeypatch, memdb) -> None:
    from tests.test_wiki import APP, FakeClient
    from wiki import pages as pages_mod
    from wiki.batch import Runner
    from wiki.build import build

    good = tmp_path / "m" / "argus-good"
    good.mkdir(parents=True)
    (good / "app.py").write_text(APP + "\nVOLUPTAS_URL = 'http://192.168.1.198:8188'  # on Jarrah\n")
    (good / "README.md").write_text("# good\nRenders via voluptas.\n")

    class _Cfg:
        repos = [RepoConfig("argus-good", str(good)), RepoConfig("argus-private", str(good)),
                 RepoConfig("voluptas", str(good))]
        output_dir = str(tmp_path / "reports")

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    eco = tmp_path / "ecosystem.md"
    eco.write_text(ECO)
    monkeypatch.setattr(pages_mod, "ECOSYSTEM_DOC", eco)
    (tmp_path / "deny.txt").write_text("Jarrah\n")
    vis = {"argus-good": "public", "argus-private": "private", "voluptas": "private"}.get
    out = tmp_path / "reports"
    fake = FakeClient()
    rep = build(None, out, runner=Runner(out, client=fake, poll_s=0), public=True,
                denylist_path=tmp_path / "deny.txt", visibility=vis)

    assert rep.repos["argus-private"].skipped == "not a public argus repo"
    assert rep.repos["voluptas"].skipped == "not a public argus repo"
    page_reqs = [r for batch in fake.batches_created for r in batch if r["params"]["model"].startswith("claude-opus")]
    assert page_reqs and all(pages_mod.PUBLIC_RULES in r["params"]["system"][0]["text"] for r in page_reqs)
    sent = json.dumps(page_reqs)
    for leak in ("voluptas", "Jarrah", "192.168.1.198"):
        assert leak.lower() not in sent.lower(), leak
    d = out / "wiki" / "_public" / "argus-good"
    idx = json.loads((d / "INDEX.json").read_text())
    assert idx["variant"] == "public" and [p["slug"] for p in idx["pages"]] == list(pages_mod.PAGES)
    assert not (d / "findings.md").exists() and not (out / "wiki" / "argus-good").exists()  # internal wiki untouched

    # the export picks the public variant
    rep2 = public.export(out, denylist_path=tmp_path / "deny.txt", visibility=vis)
    assert rep2.exported == {"argus-good": list(pages_mod.PAGES)}



def test_bundle_hash_ignores_manifest_timestamp(wiki: Path) -> None:
    deny = wiki.parent / "deny.txt"
    first = public.export(wiki, denylist_path=deny, visibility=VIS.get).bundle_sha256
    second = public.export(wiki, denylist_path=deny, visibility=VIS.get).bundle_sha256
    assert first == second  # nothing changed, so no new commit or pin bump


# ── publish to the docs repo ─────────────────────────────────────────────────

def _bare(tmp_path: Path) -> str:
    import subprocess

    remote = tmp_path / "argus-docs.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    return str(remote)


def test_publish_commits_as_bot_only_on_change(wiki: Path, tmp_path: Path) -> None:
    import subprocess

    from wiki import publish as pub

    deny = wiki.parent / "deny.txt"
    public.export(wiki, denylist_path=deny, visibility=VIS.get)
    remote, co = _bare(tmp_path), tmp_path / "co"
    first = pub.publish(wiki / "wiki_public", repo=remote, checkout=co, push=True, denylist_path=deny, visibility=VIS.get)
    assert first["changed"] and first["pushed"] and first["pin"]["commit"] == first["commit"]
    log = subprocess.run(["git", "-C", remote, "log", "-1", "--format=%an <%ae>%n%B"], capture_output=True, text=True).stdout
    assert log.startswith("argus-docs-bot <noreply@dragonhound.dev>") and first["bundle_sha256"] in log
    assert (co / "argus-good" / "overview.html").exists() and not (co / "argus-leaky").exists()

    public.export(wiki, denylist_path=deny, visibility=VIS.get)  # same content, new timestamp
    again = pub.publish(wiki / "wiki_public", repo=remote, checkout=co, push=True, denylist_path=deny, visibility=VIS.get)
    assert not again["changed"] and not again["pushed"] and again["commit"] == first["commit"]

    (wiki / "wiki" / "argus-good" / "interfaces.md").write_text("# Interfaces\n\nCLI and API.\n")
    public.export(wiki, denylist_path=deny, visibility=VIS.get)
    third = pub.publish(wiki / "wiki_public", repo=remote, checkout=co, push=False, denylist_path=deny, visibility=VIS.get)
    assert third["changed"] and not third["pushed"] and third["commit"] != first["commit"]
    remote_head = subprocess.run(["git", "-C", remote, "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    assert remote_head == first["commit"]  # not pushed without --push


def test_publish_refuses_tampered_or_leaky_bundles(wiki: Path, tmp_path: Path) -> None:
    from wiki import publish as pub

    deny = wiki.parent / "deny.txt"
    public.export(wiki, denylist_path=deny, visibility=VIS.get)
    b = wiki / "wiki_public"
    (b / "argus-good" / "overview.html").write_text("<p>edited after export</p>")
    with pytest.raises(RuntimeError, match="bundle hash mismatch"):
        pub.publish(b, repo=_bare(tmp_path), checkout=tmp_path / "co", denylist_path=deny, visibility=VIS.get)
    public.export(wiki, denylist_path=deny, visibility=VIS.get)
    (deny).write_text("Jarrah\nCLI\n")  # a term that now appears in the bundle
    with pytest.raises(RuntimeError, match="opsec gate failed"):
        pub.publish(b, repo=_bare(tmp_path), checkout=tmp_path / "co2", denylist_path=deny, visibility=VIS.get)
