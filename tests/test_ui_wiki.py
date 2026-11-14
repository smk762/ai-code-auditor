from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor import ui_wiki
from auditor.config import RepoConfig

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin"}


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=ENV)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", name], check=True, env=ENV)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch) -> Path:
    out = tmp_path / "reports"
    svc = tmp_path / "mirrors" / "svc"
    svc.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(svc)], check=True, env=ENV)
    built = _commit(svc, "a.py")
    _commit(svc, "b.py")
    tip = _commit(svc, "c.py")
    yamlonly = tmp_path / "mirrors" / "ci"
    yamlonly.mkdir()
    (yamlonly / "ci.yml").write_text("on: push\n")

    class _Cfg:
        repos = [RepoConfig("svc", str(svc), subpath=""), RepoConfig("other", str(svc)),
                 RepoConfig("ci", str(yamlonly)), RepoConfig("off", "/x", enabled=False)]
        output_dir = str(out)

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    monkeypatch.delenv("AI_AUDIT_MERGE_POLL_STATE", raising=False)
    d = out / "wiki" / "svc"
    d.mkdir(parents=True)
    (d / "overview.md").write_text(
        "# svc overview\n\nThe app is built by `create_app` [app.py:L9-L11] and [app.py:L3].\n\n"
        "```mermaid\ngraph TD; A-->B [app.py:L1]\n```\n")
    (d / "findings.md").write_text("# Notable findings\n\nNone.\n")
    (d / "INDEX.json").write_text(json.dumps({
        "repo": "svc", "branch": "dev", "source_sha": built, "built_at": "2026-11-14T05:00:00+00:00",
        "pages": [
            {"slug": "overview", "title": "Overview", "file": "overview.md", "generator": "llm", "model": "claude-opus-5-5",
             "generated_at": "2026-11-14T05:00:00+00:00", "source_sha": built,
             "citations": {"total": 3, "valid": 3, "named": 1, "matched": 1, "failed": []},
             "dropped_claims": [{"claim": "It uses `ghost` [app.py:L1].", "reason": "symbol", "citation": "[app.py:L1]"}]},
            {"slug": "findings", "title": "Notable findings", "file": "findings.md", "generator": "data",
             "citations": {"total": 0}, "stale": False},
            {"slug": "evil", "title": "Evil", "file": "../../../etc/passwd", "generator": "llm"},
        ]}))
    (out / "wiki" / "INDEX.json").write_text(json.dumps({"updated_at": "2026-11-14T05:00:00+00:00",
                                                          "repos": {"svc": {}, "gone": {}}}))
    (out / "merge_poll_state.json").write_text(json.dumps({"repos": {
        "svc": {"sha": tip, "slug": "o/svc"}}}))
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    return out


def test_link_citations_outside_code_fences() -> None:
    md = "x [a/b.py:L3-L9] y [c.py:L4]\n```\nkeep [a/b.py:L1]\n```\n"
    out = ui_wiki.link_citations(md, "o/r", "abc", "pkg")
    assert "[`a/b.py:L3-L9`](https://github.com/o/r/blob/abc/pkg/a/b.py#L3-L9)" in out
    assert "[`c.py:L4`](https://github.com/o/r/blob/abc/pkg/c.py#L4)" in out
    assert "keep [a/b.py:L1]" in out
    assert ui_wiki.link_citations("[c.py:L4]", None, "abc") == "`c.py:L4`"  # no slug: plain code


def test_wiki_repos_freshness_and_missing_reasons(wiki: Path) -> None:
    d = ui_wiki.wiki_repos(wiki)
    assert [r["repo"] for r in d["repos"]] == ["svc"]  # "gone" has no INDEX: skipped
    r = d["repos"][0]
    assert r["behind"] is True and r["commits"] == 2 and r["github_url"] == "https://github.com/o/svc"
    assert r["citations"] == 3 and r["dropped_claims"] == 1
    reasons = {m["repo"]: m["reason"] for m in d["missing"]}
    assert set(reasons) == {"other", "ci"}  # disabled repos are not listed
    assert reasons["ci"].startswith("no Python, JS or TS source") and reasons["other"] == "not built yet"


def test_behind_states(tmp_path: Path) -> None:
    assert ui_wiki._behind(None, "a", "a") == {"behind": False, "commits": 0, "tip_sha": "a"}
    assert ui_wiki._behind(None, "a", None)["behind"] is None
    assert ui_wiki._behind(RepoConfig("x", str(tmp_path)), "a", "b") == {"behind": True, "commits": None, "tip_sha": "b"}


def test_page_render(wiki: Path) -> None:
    page = ui_wiki.render_wiki_page(wiki, "svc", "overview")
    assert '<a href="/ui/wiki/svc/overview" aria-current="page">Overview</a>' in page
    assert 'aria-current="page">Wiki</a>' in page  # top nav
    assert "/blob/" in page and "app.py#L9-L11" in page
    assert "2 commits behind dev" in page and ">GitHub</a>" in page
    assert '<code class="language-mermaid">' in page and "[app.py:L1]" in page  # fenced: unlinked
    assert "1 claim dropped by the citation check" in page and "ghost" in page
    data = ui_wiki.render_wiki_page(wiki, "svc", "findings")
    assert "Built from the audit data" in data
    assert ui_wiki.render_wiki_page(wiki, "svc", "nope") is None
    assert ui_wiki.render_wiki_page(wiki, "svc", "evil") is None  # file outside the repo dir
    assert ui_wiki.render_wiki_page(wiki, "nope", "overview") is None


def test_home_render(wiki: Path) -> None:
    home = ui_wiki.render_wiki_home(wiki)
    assert '<a href="/ui/wiki/svc/overview">svc</a>' in home
    assert "Without a wiki" in home and "no Python, JS or TS source" in home


def test_routes(client: TestClient, wiki: Path) -> None:
    assert client.get("/ui/wiki").status_code == 200
    r = client.get("/ui/wiki/svc", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/wiki/svc/overview"
    assert client.get("/ui/wiki/svc/overview").status_code == 200
    assert client.get("/ui/wiki/svc/evil").status_code == 404
    assert client.get("/ui/wiki/nope").status_code == 404
    assert client.get("/audit/wiki").json()["repos"][0]["repo"] == "svc"
    assert client.get("/audit/wiki/svc").json()["github_slug"] == "o/svc"
    p = client.get("/audit/wiki/svc/overview").json()
    assert p["markdown"].startswith("# svc overview") and "[app.py:L9-L11]" in p["markdown"]
    assert client.get("/audit/wiki/svc/nope").status_code == 404
