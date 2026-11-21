"""ui_wiki._behind: a repo with no configured path never runs a command in the process cwd."""
from __future__ import annotations

from types import SimpleNamespace

from auditor import ui_wiki


def _fake_run(calls):
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="3\n")
    return run


def test_no_configured_path_runs_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ui_wiki.subprocess, "run", _fake_run(calls))
    for cfg in (None, SimpleNamespace(path=None), SimpleNamespace(path="")):
        assert ui_wiki._behind(cfg, "aaaa", "bbbb") == {"behind": True, "commits": None, "tip_sha": "bbbb"}
    assert calls == []


def test_configured_path_counts_commits(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(ui_wiki.subprocess, "run", _fake_run(calls))
    assert ui_wiki._behind(SimpleNamespace(path=str(tmp_path)), "aaaa", "bbbb")["commits"] == 3
    assert calls[0][1:3] == ["-C", str(tmp_path)]
