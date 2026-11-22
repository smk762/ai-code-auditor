"""Workaround comments, copy risk and consolidation-first ordering (#60)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auditor import workaround as w
from auditor.agent_pack import consolidation_first
from auditor.extraction import annotate_copy_risk
from auditor.finding_class import classify, static_rule_id

# ── the detector ─────────────────────────────────────────────────────────────

FLAGGED = [
    "# HACK: the SDK drops the header, so we set it twice",
    "# FIXME look at relaxing size constraints",
    "# Workaround for the driver's broken pool reset",
    "# temporary fix until the scheduler handles retries",
    "# TODO: remove after the v2 clients are gone",
    "# remove this once every caller passes a timeout",
    "# kept until upstream ships the fix (encode/httpx#123)",
    "# Backwards-compat shim: callers that imported get_pipeline",
]
NOT_FLAGGED = [
    "# The lock is held until the DDL commits and is released",       # 'until … released': a lock
    "# none is served until it is fixed or deleted",                  # describes behaviour
    "# Studio's own workaround is gone, so this checks the real page",  # records a removal
    "# a TODO/FIXME comment marker (not a secret)",                   # mentions markers
    "# stub via monkeypatch of loaders",                              # the pytest fixture
    "# Workaround: retries the upload once.  workaround: ok — the API is documented as at-least-once",
    "# a hack in prose, lower case, is not a marker",
]


@pytest.mark.parametrize("line", FLAGGED)
def test_workaround_comments_are_flagged(line: str) -> None:
    assert w.classify(line.lstrip("# "), [line], 1) == "marker"


@pytest.mark.parametrize("line", NOT_FLAGGED)
def test_descriptions_are_not_flagged(line: str) -> None:
    assert w.classify(line.lstrip("# "), [line], 1) == ""


def test_a_race_timer_needs_the_sleep_next_to_it() -> None:
    lines = ["# give the worker time to finish, the race is rare", "time.sleep(0.5)", "x = 1"]
    assert w.classify(lines[0].lstrip("# "), lines, 1) == "race_timer"
    assert w.classify(lines[0].lstrip("# "), [lines[0], "x = 1"], 1) == ""
    # a sleep without a race excuse is not a workaround comment
    assert w.classify("wait for the user to read it", ["# wait for the user", "time.sleep(1)"], 1) == ""


def test_scan_reads_comments_not_strings_and_skips_vendored_code(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text(
        'MSG = "HACK: not a comment"\n'
        "def f():\n"
        "    return 1  # HACK: the SDK drops the header\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "b.ts").write_text(
        'const url = "http://x//HACK";\n'
        "// FIXME: drop this once the API returns ids\n"
        "/* Workaround: Safari loses the focus\n"
        "   on blur */\n")
    for skipped in ("node_modules", "vendor", ".venv"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "c.js").write_text("// HACK: vendored\n")
    hits = w.scan("r", tmp_path)
    assert sorted(f.id for f in hits) == ["workaround:marker:app/a.py:3", "workaround:marker:web/b.ts:2",
                                          "workaround:marker:web/b.ts:3"]
    f = next(h for h in hits if h.id.endswith("a.py:3"))
    assert (f.source, f.severity, f.repo) == ("workaround", "LOW", "r")
    assert f.title.startswith("Workaround comment: HACK: the SDK")
    assert "3|     return 1  # HACK" in f.evidence
    # diff audits pass the changed files
    assert [h.id for h in w.scan("r", tmp_path, ["app/a.py", "README.md"])] == ["workaround:marker:app/a.py:3"]


def test_a_file_that_does_not_tokenize_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text('x = """never closed\n# HACK: inside a string\n')
    assert w.scan("r", tmp_path) == []


def test_findings_map_to_the_workaround_class() -> None:
    for fid in ("workaround:marker:a.py:3", "workaround:race_timer:a.py:3"):
        m = classify("anything", source="workaround", rule_id=static_rule_id(fid, "workaround"))
        assert m.cls == "workaround" and m.rule.startswith("static:workaround:")
    # and by title, for packs whose source the class step can't see
    assert classify("Timer or retry papering over a race: give it time").cls == "workaround"


# ── copy risk ────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}).stdout.strip()


def test_copy_risk_counts_code_copies_churn_and_agent_prs(tmp_path: Path) -> None:
    repo = tmp_path / "argus-a"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "src" / "h.py").write_text("def _now(): pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    cand = {"source_units": [{"repo": "argus-a", "path": str(repo / "src/h.py"), "symbols": ["_now"]},
                             {"repo": "argus-b", "path": "/m/argus-b/src/t.py", "symbols": ["_now"]},
                             {"repo": "argus-b", "path": "/m/argus-b/tests/test_t.py", "symbols": ["_now"]}],
            "score_breakdown": {"churn_score": 0.5}}
    prs = [{"repo": "argus-a", "number": 7, "url": "u7", "commit_shas": [sha]}]
    [c] = annotate_copy_risk([cand], {"argus-a": str(repo)}, prs)
    assert c["copy_risk"] == {"score": round(0.4 * 2 / 8 + 0.3 * 0.5 + 0.3 / 3, 3), "copies": 2,
                              "churn": 0.5, "agent_prs": 1}
    [c] = annotate_copy_risk([dict(cand)], {"argus-a": str(repo)}, None)  # no PR cache: no agent signal
    assert c["copy_risk"]["agent_prs"] == 0


# ── consolidation first ──────────────────────────────────────────────────────

def _e(pid: str, *, kind: str = "finding", path: str = "", title: str = "t", cls: str = "", areas=None) -> dict:
    e = {"id": pid, "kind": kind, "repo": "r", "file_path": path, "title": title, "finding_class": cls}
    if areas is not None:
        e["areas"] = areas
    return e


def test_consolidation_moves_ahead_of_a_fix_in_its_area_only() -> None:
    ranked = [
        (3.0, _e("fix-other", path="/m/r/app/other/x.py")),
        (2.0, _e("fix-api", path="/m/r/app/api/a.py")),
        (1.5, _e("fix-api-2", path="/m/r/app/api/b.py")),
        (0.5, _e("wa-api", path="/m/r/app/api/c.py", title="Workaround comment: HACK")),
        (0.4, _e("ext", kind="extraction", areas=["r/app/other"])),
        (0.3, _e("wa-lonely", path="/m/r/app/lonely/d.py", cls="workaround")),
    ]
    assert [e["id"] for _, e in consolidation_first(ranked)] == [
        "ext", "fix-other", "wa-api", "fix-api", "fix-api-2", "wa-lonely"]


def test_an_extraction_index_entry_lists_the_areas_of_its_remaining_copies() -> None:
    from auditor.extraction import build_pack

    copies = [{"repo": "argus-a", "path": "src/a/h.py", "symbol": "s", "state": "left"},
              {"repo": "argus-b", "path": "x/y.py", "symbol": "s", "state": "gone"}]
    pack = build_pack("ext-1", {"target_repo": "argus-cortex", "symbol": "s"}, copies)
    assert pack.to_index_entry(path="p")["areas"] == ["argus-a/src/a"]
