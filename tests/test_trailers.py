"""Commit trailers (#73): parsing (agreeing with git), rendering, the PR table, pack-level revert
attribution on synthetic git history, the PR template and issue form."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from auditor import pack_awareness as pa
from auditor import trailers as tr
from auditor import trust_metrics as tm
from auditor.agent_pack import packs_dir

FIX = "Fix path join in upload handler\n\nUse safe_join so names can't climb out.\n\n" \
      "Pack: pack-argus-lens-0a9ba5beb5\nPack: pack-argus-lens-77c1d2e3f4\nEvidence: repro\nClass: path_traversal\n" \
      "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"


def test_parse_reads_only_the_final_trailer_block() -> None:
    t = tr.parse(FIX)
    assert t["Pack"] == ["pack-argus-lens-0a9ba5beb5", "pack-argus-lens-77c1d2e3f4"]
    assert t["Evidence"] == ["repro"] and t["Class"] == ["path_traversal"]
    assert tr.pack_ids(FIX) == ["pack-argus-lens-0a9ba5beb5", "pack-argus-lens-77c1d2e3f4"]
    assert tr.parse("Subject only\n") == {}
    assert tr.parse("Subject\n\nNote: this body line is prose\nnot a trailer block") == {}
    assert tr.parse("Subject\n\nPack: pack-x-1\n\nThe end.") == {}       # trailers must be last
    assert tr.parse("Subject\n\npack: pack-a-1\nWaiver: no test can\n  reach the GPU path")["Waiver"] == \
        ["no test can reach the GPU path"]                               # any case; continuation lines
    assert tr.pack_ids("S\n\nPack: not a pack id\nPack: pack-ok-1") == ["pack-ok-1"]


@pytest.mark.parametrize("msg", [FIX, "Subject\n\nBody text.\n\nPack: pack-a-b-1\nEvidence: tests\n",
                                 "Subject\n\nNote: prose here\nstill prose\n", "Subject only\n"])
def test_parse_agrees_with_git(msg: str) -> None:
    out = subprocess.run(["git", "interpret-trailers", "--parse"], input=msg, capture_output=True, text=True,
                         check=True).stdout
    from_git = [line.split(": ", 1)[1] for line in out.splitlines() if line.lower().startswith("pack: ")]
    assert tr.pack_ids(msg) == [v for v in from_git if v.startswith("pack-")]


def pack(out: Path, pid: str, *, cls: str = "", ev: dict | None = None, status: str = "resolved",
         title: str = "Upload name | climbs out") -> None:
    d = packs_dir(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.yaml").write_text(json.dumps({
        "work_item_id": pid, "kind": "nightly", "status": status, "repo": {"name": "argus-lens"},
        "narrative": {"title": title}, "governance": {"finding_class": {"id": cls}} if cls else {},
        "verification": {"evidence": ev} if ev else {}}))


def test_render_trailers_from_the_pack_store(tmp_path: Path) -> None:
    pack(tmp_path, "pack-l-1", cls="path_traversal", ev={"kind": "repro", "repro": {"repro": ["t::a"]}})
    pack(tmp_path, "pack-l-2", cls="path_traversal", ev={"kind": "waiver", "waiver": "GPU only\npath"})
    block = tr.render(tmp_path, ["pack-l-1", "pack-l-2", "pack-l-1"])
    assert block.splitlines() == ["Pack: pack-l-1", "Pack: pack-l-2", "Evidence: repro", "Evidence: waiver",
                                  "Class: path_traversal", "Waiver: GPU only path"]
    assert tr.render(tmp_path, ["pack-l-1"], evidence="tests").splitlines()[1] == "Evidence: tests"
    assert tr.parse(f"Fix\n\n{block}")["Pack"] == ["pack-l-1", "pack-l-2"]   # what we write, we read
    with pytest.raises(ValueError, match="not pack ids"):
        tr.render(tmp_path, ["rm -rf /"])
    with pytest.raises(ValueError, match="evidence must be"):
        tr.render(tmp_path, ["pack-l-1"], evidence="vibes")


def test_summary_table(tmp_path: Path) -> None:
    pack(tmp_path, "pack-l-1", cls="path_traversal", ev={"kind": "repro", "repro": {"repro": ["tests/t.py::a"]}})
    md = tr.summary_markdown(tmp_path, ["pack-l-1", "pack-gone-9"], public_base_url="http://h:1")
    assert "| [`pack-l-1`](http://h:1/ui/packs/pack-l-1) Upload name \\| climbs out | path_traversal | " \
           "repro (`tests/t.py::a`) | resolved |" in md
    assert "| `pack-gone-9` |" not in md and "not found" in md
    assert "fixes no audit packs" in tr.summary_markdown(tmp_path, [])


def git(repo: Path, *args: str, msg: str = "") -> str:
    a = list(args) + (["-m", msg] if msg else [])
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "dev")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a\n")
    git(r, "add", ".")
    git(r, "commit", "-q", msg="base")
    return r


def commit(r: Path, name: str, msg: str) -> str:
    (r / name).write_text(name)
    git(r, "add", ".")
    git(r, "commit", "-q", msg=msg)
    return git(r, "rev-parse", "HEAD")


def test_range_pack_ids(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "x", "Fix x\n\nPack: pack-r-1")
    commit(repo, "y", "Fix y\n\nPack: pack-r-2\nPack: pack-r-1")
    assert tr.range_pack_ids(repo, f"{base}..HEAD") == ["pack-r-2", "pack-r-1"]


def test_reverts_are_attributed_to_the_named_packs(repo: Path) -> None:
    keep = commit(repo, "k", "Fix k\n\nPack: pack-r-keep")
    bad = commit(repo, "b", "Fix b\n\nPack: pack-r-bad\nPack: pack-r-bad2")
    git(repo, "revert", "--no-edit", bad)
    # A feature branch merged with a merge commit, then the merge reverted.
    git(repo, "checkout", "-q", "-b", "feat")
    commit(repo, "m", "Fix m\n\nPack: pack-r-merged")
    git(repo, "checkout", "-q", "dev")
    git(repo, "merge", "-q", "--no-ff", "feat", msg="Merge feat")
    merge = git(repo, "rev-parse", "HEAD")
    git(repo, "revert", "--no-edit", "-m", "1", merge)
    m = tm.MirrorCommits(repo)
    commits = {"r": m.commits("2000-01-01")}
    rep = tm.pack_commit_metric(commits, lambda _r, c: m.merged_by(c), now=datetime.now(UTC))
    assert rep["packs"] == 4 and rep["commits"] == 3
    assert {x["pack"] for x in rep["reverted_packs"]} == {"pack-r-bad", "pack-r-bad2", "pack-r-merged"}
    assert rep["rate"] == 0.75 and rep["mature"] == 0 and keep


def test_revert_outside_the_window_doesnt_count() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    commits = {"r": [
        {"sha": "a" * 40, "parents": [], "date": t0.isoformat(), "message": "Fix\n\nPack: pack-r-1"},
        {"sha": "b" * 40, "parents": [], "date": (t0 + timedelta(days=20)).isoformat(),
         "message": f'Revert "Fix"\n\nThis reverts commit {"a" * 40}.'},
    ]}
    rep = tm.pack_commit_metric(commits, lambda *_: [], now=t0 + timedelta(days=30))
    assert rep["reverted"] == 0 and rep["mature"] == 1 and rep["mature_rate"] == 0.0


def test_github_pr_with_trailers_counts_whatever_its_title() -> None:
    node = {"number": 5, "title": "Tidy upload handling", "body": "", "url": "u", "mergedAt": "2026-11-01T00:00:00Z",
            "repository": {"name": "argus-lens", "nameWithOwner": "o/argus-lens"}, "mergeCommit": {"oid": "m"},
            "commits": {"nodes": [{"commit": {"oid": "c1", "message": "Tidy\n\nPack: pack-argus-lens-0a9ba5beb5"}}]}}
    plain = {**node, "url": "u2", "commits": {"nodes": [{"commit": {"oid": "c2", "message": "Tidy"}}]}}
    gql = lambda q, v: {"search": {"nodes": [node, plain], "pageInfo": {"hasNextPage": False}}}
    prs = tm.fetch_agent_prs(gql, "o")
    assert [p["url"] for p in prs] == ["u"] and prs[0]["pack_ids"] == ["pack-argus-lens-0a9ba5beb5"]
    assert prs[0]["trailers"] is True


def test_issue_form_is_valid_and_its_answers_parse() -> None:
    form = yaml.safe_load(pa.render_issue_form())
    ids = [b["id"] for b in form["body"]]
    assert ids == ["pack", "location", "class", "evidence", "what"]
    assert "ssrf" in next(b for b in form["body"] if b["id"] == "class")["attributes"]["options"]
    body = "### Pack id\n\npack-l-1\n\n### Location\n\na.py:3\n\n### Finding class\n\nssrf\n\n" \
           "### Evidence\n\n_No response_\n\n### What is wrong\n\nIt fetches\nany URL."
    f = tr.issue_fields(body)
    assert f == {"Pack id": "pack-l-1", "Location": "a.py:3", "Finding class": "ssrf", "Evidence": "",
                 "What is wrong": "It fetches\nany URL."}


def test_installer_adds_template_and_form_and_keeps_user_files(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "pull_request_template.md").write_text("## Summary\n\nWhat changed.\n")
    r = pa.install_into_repo("argus-lens", tmp_path)
    tpl = (tmp_path / pa.PR_TEMPLATE_RELPATH).read_text()
    assert tpl.startswith("## Summary\n\nWhat changed.\n\n") and "### Audit packs" in tpl
    assert (tmp_path / pa.ISSUE_FORM_RELPATH).exists() and r.changed
    again = pa.install_into_repo("argus-lens", tmp_path)
    assert not again.changed and (tmp_path / pa.PR_TEMPLATE_RELPATH).read_text() == tpl   # idempotent
    (tmp_path / pa.ISSUE_FORM_RELPATH).write_text("name: theirs\n")
    res = pa.install_into_repo("argus-lens", tmp_path)
    assert any("audit-finding.yml: skipped" in a for a in res.actions)
    assert (tmp_path / pa.ISSUE_FORM_RELPATH).read_text() == "name: theirs\n"


def test_cursor_rule_without_marker_no_longer_skips_the_rest(tmp_path: Path) -> None:
    rule = tmp_path / pa.RULE_RELPATH
    rule.parent.mkdir(parents=True)
    rule.write_text("my own rule\n")
    res = pa.install_into_repo("argus-lens", tmp_path)
    assert (tmp_path / pa.PR_TEMPLATE_RELPATH).exists() and rule.read_text() == "my own rule\n"
    assert any("skipped" in a for a in res.actions)


def test_api_pr_summary(client, tmp_path: Path) -> None:
    pack(tmp_path, "pack-l-1", cls="ssrf", ev={"kind": "tests"})
    r = client.get("/audit/pr-summary", params={"packs": "pack-l-1", "evidence": "repro"})
    assert r.status_code == 200
    assert r.json()["trailers"] == "Pack: pack-l-1\nEvidence: repro\nClass: ssrf"
    assert "| ssrf | tests | resolved |" in r.json()["markdown"]
    assert client.get("/audit/pr-summary", params={"packs": "x;y"}).status_code == 422
