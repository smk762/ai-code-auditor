"""The write-time guard hook (auditor/guard_hook.py): advice on the lines an agent changed, from proven
rules only, never evidence, and never blocking an edit when it can't run."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from auditor import guard_hook as gh
from auditor import pack_awareness as pa

OLD = 'import requests\n\n\ndef old():\n    return requests.get("http://x")\n'
NEW = OLD + '\n\ndef new():\n    return requests.post("http://y")\n'


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "m.py").write_text(OLD)
    _git(root, "add", "m.py")
    _git(root, "commit", "-qm", "init")
    (root / "m.py").write_text(NEW)
    return root


def _suite_at(*lines: int):
    """A suite runner reporting missing-timeout at *lines*."""
    def run(repo, root, rel, out):
        return [gh.Hit("suite:missing-timeout", n, "An HTTP call without a timeout", "requests.x()") for n in lines]
    return run


def _none(*_a):
    return None


def test_hits_outside_the_diff_are_not_shown(repo: Path, tmp_path: Path) -> None:
    hits, row = gh.check(str(repo / "m.py"), str(repo), out=tmp_path / "out", suite=_suite_at(5, 9), rules=[])
    assert [h.line for h in hits] == [9]  # line 5 is committed debt
    assert row["status"] == "ok"
    assert row["rules"] == {"suite:missing-timeout": {"total": 2, "shown": 1}}


def test_a_new_untracked_file_shows_every_hit(repo: Path, tmp_path: Path) -> None:
    (repo / "fresh.py").write_text(NEW)
    hits, _ = gh.check(str(repo / "fresh.py"), str(repo), out=tmp_path / "out", suite=_suite_at(5, 9), rules=[])
    assert [h.line for h in hits] == [5, 9]


def test_a_rule_that_cannot_run_shows_nothing_and_exits_zero(repo: Path, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "out"
    hits, row = gh.check(str(repo / "m.py"), str(repo), out=out, suite=_none, rules=[])
    assert hits == [] and row["status"] == "unavailable"
    monkeypatch.setattr(gh, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    payload = json.dumps({"tool_input": {"file_path": str(repo / "m.py")}, "cwd": str(repo)})
    assert gh.main(io.StringIO(payload)) == 0
    assert gh.main(io.StringIO("not json")) == 0
    assert gh.main(io.StringIO(json.dumps({"tool_input": {}}))) == 0


def test_hits_exit_two_with_the_message_on_stderr(repo: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    real = gh.check
    monkeypatch.setattr(gh, "check", lambda fp, cwd: real(fp, cwd, out=tmp_path / "out", suite=_suite_at(9), rules=[]))
    payload = json.dumps({"tool_input": {"file_path": str(repo / "m.py")}, "cwd": str(repo)})
    assert gh.main(io.StringIO(payload)) == 2
    err = capsys.readouterr().err
    assert "m.py:9 [suite:missing-timeout]" in err and "noqa" in err


def test_non_python_files_are_not_checked(repo: Path, tmp_path: Path) -> None:
    (repo / "README.md").write_text("x")
    hits, row = gh.check(str(repo / "README.md"), str(repo), out=tmp_path / "out", suite=_suite_at(1), rules=[])
    assert hits == [] and "status" not in row
    assert not (tmp_path / "out" / gh.HISTORY_NAME).exists()


def _class_pack(out: Path, cls: str, guards: dict) -> None:
    d = out / "agent_packs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"pack-class-{cls.replace('_', '-')}.yaml").write_text(yaml.safe_dump(
        {"id": f"pack-class-{cls}", "kind": "class_guard",
         "governance": {"finding_class": {"id": cls}, "class_guard": guards}}))


def test_an_unproven_guards_rule_is_never_loaded(tmp_path: Path) -> None:
    rule = {"tool": "ruff", "select": ["S113"]}
    _class_pack(tmp_path, "missing_timeout", {
        "argus-a": {"commit": "a" * 40, "proof_test": "t.py", "rule": rule, "proof": {"status": "pending"}},
        "argus-b": {"commit": "b" * 40, "proof_test": "t.py", "rule": rule, "proof": {"status": "failed"}}})
    _class_pack(tmp_path, "blocking_io_async", {
        "argus-c": {"commit": "c" * 40, "proof_test": "t.py", "proof": {"status": "proven"}}})  # no rule
    assert gh.proven_rules(tmp_path) == []
    _class_pack(tmp_path, "missing_timeout", {
        "argus-a": {"commit": "a" * 40, "proof_test": "t.py", "rule": rule, "proof": {"status": "proven"}},
        "argus-b": {"commit": "b" * 40, "proof_test": "t.py", "rule": rule, "proof": {"status": "proven"}}})
    assert gh.proven_rules(tmp_path) == [{"tool": "ruff", "select": ["S113"], "class": "missing_timeout"}]


@pytest.mark.skipif(not (shutil.which("ruff") or Path(__import__("sys").executable).with_name("ruff").is_file()),
                    reason="ruff not installed")
def test_a_proven_class_guard_rule_runs_and_a_failed_suite_makes_the_run_partial(repo: Path, tmp_path: Path) -> None:
    rule = {"tool": "ruff", "select": ["S113"], "class": "missing_timeout"}
    hits, row = gh.check(str(repo / "m.py"), str(repo), out=tmp_path / "out", suite=_none, rules=[rule])
    assert [(h.rule, h.line) for h in hits] == [("ruff:S113", 9)]
    assert row["status"] == "partial"  # the suite rules didn't run: their counts are missing, not zero


def test_the_hook_never_writes_a_pack(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _class_pack(out, "missing_timeout", {})
    before = {p: p.read_bytes() for p in (out / "agent_packs").rglob("*")}
    gh.check(str(repo / "m.py"), str(repo), out=out, suite=_suite_at(9))
    assert {p: p.read_bytes() for p in (out / "agent_packs").rglob("*")} == before
    assert sorted(p.name for p in out.iterdir()) == ["agent_packs", gh.HISTORY_NAME]


def test_nothing_that_resolves_or_closes_packs_reads_the_hook() -> None:
    root = Path(gh.__file__).resolve().parent
    for mod in ("evidence", "validator", "class_packs", "pack_gc", "revalidate", "agent_pack", "trust_metrics",
                "autonomy", "extraction", "structure_packs"):
        src = (root / f"{mod}.py").read_text(encoding="utf-8")
        assert "guard_hook" not in src, f"{mod}.py reads the write-time hook: it is advice, never evidence"


# ── the metric ───────────────────────────────────────────────────────────────

def _row(at: datetime, status: str = "ok", total: int = 1, shown: int = 0, file: str = "m.py") -> dict:
    r = {"at": at.isoformat(), "repo": "demo", "file": file, "status": status}
    if status != "unavailable":
        r["rules"] = {"suite:missing-timeout": {"total": total, "shown": shown}} if total else {}
    return r


def _history(out: Path, rows: list[dict]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / gh.HISTORY_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_a_hit_counts_as_handled_only_when_the_whole_file_count_falls(tmp_path: Path) -> None:
    t = datetime.now(UTC) - timedelta(hours=1)
    m = timedelta(minutes=1)
    _history(tmp_path, [
        _row(t, total=2, shown=1), _row(t + m, total=1, shown=0),          # fixed: 2 -> 1
        _row(t, total=1, shown=1, file="b.py"), _row(t + m, total=1, file="b.py"),  # committed: still there
        _row(t, total=1, shown=1, file="c.py"),                            # no later check: not counted
    ])
    m_ = gh.write_time_metric(tmp_path)
    assert (m_["n"], m_["handled"], m_["rate"]) == (2, 1, 0.5)


def test_an_unavailable_or_partial_run_never_reads_as_a_fix(tmp_path: Path) -> None:
    t = datetime.now(UTC) - timedelta(hours=1)
    m = timedelta(minutes=1)
    _history(tmp_path, [
        _row(t, total=1, shown=1), _row(t + m, status="unavailable"), _row(t + 2 * m, status="partial", total=0),
        _row(t + 3 * m, total=1),
    ])
    m_ = gh.write_time_metric(tmp_path)
    assert (m_["n"], m_["handled"]) == (1, 0)


def test_old_history_is_outside_the_window(tmp_path: Path) -> None:
    t = datetime.now(UTC) - timedelta(days=gh.WINDOW_DAYS + 1)
    _history(tmp_path, [_row(t, total=1, shown=1), _row(t + timedelta(minutes=1), total=0)])
    assert gh.write_time_metric(tmp_path)["n"] == 0


def test_the_write_time_gate_is_informational(tmp_path: Path) -> None:
    from auditor import trust_eval

    assert "guards.write_time" in trust_eval.INFORMATIONAL
    gates = {g.id: g for g in trust_eval.build_gates({}, None, guard_hook={"n": 25, "rate": 0.9})}
    assert gates["guards.write_time"].status == "pass"
    gates = {g.id: g for g in trust_eval.build_gates({}, None)}
    assert gates["guards.write_time"].status == "insufficient"


# ── the installer ────────────────────────────────────────────────────────────

def _target(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    (root / ".git" / "info").mkdir(parents=True)
    return root


def test_install_merges_the_hook_and_keeps_the_users_settings(tmp_path: Path) -> None:
    root = _target(tmp_path)
    settings = root / pa.CLAUDE_SETTINGS_RELPATH
    settings.parent.mkdir()
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
    old_ours = {"matcher": "Edit", "hooks": [{"type": "command", "command": "/old/python -m auditor.guard_hook"}]}
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                    "hooks": {"PostToolUse": [mine, old_ours], "Stop": [mine]}}))
    pa.install_into_repo("target", root, python="/venv/bin/python")
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]} and data["hooks"]["Stop"] == [mine]
    post = data["hooks"]["PostToolUse"]
    assert post[0] == mine and len(post) == 2
    assert post[1]["matcher"] == pa.HOOK_MATCHER
    assert post[1]["hooks"][0]["command"] == "/venv/bin/python -I -m auditor.guard_hook"
    assert "/.claude/settings.local.json" in (root / ".git" / "info" / "exclude").read_text()
    again = pa.install_into_repo("target", root, python="/venv/bin/python")
    assert f"{pa.CLAUDE_SETTINGS_RELPATH}: unchanged" in again.actions
    assert (root / ".git" / "info" / "exclude").read_text().count("settings.local.json") == 1


def test_install_leaves_an_ignore_file_alone_when_it_names_the_settings(tmp_path: Path) -> None:
    root = _target(tmp_path)
    (root / ".gitignore").write_text(".claude/settings.local.json\n")
    pa.install_into_repo("target", root, python="/p")
    assert not (root / ".git" / "info" / "exclude").exists()
    assert (root / pa.CLAUDE_SETTINGS_RELPATH).exists()


def test_install_refuses_the_hook_when_it_cannot_keep_it_out_of_git(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".git").write_text("gitdir: /elsewhere\n")  # a worktree: no local exclude to write
    r = pa.install_into_repo("wt", root, python="/p")
    assert not (root / pa.CLAUDE_SETTINGS_RELPATH).exists()
    assert any("settings.local.json: skipped" in a for a in r.actions)


def test_install_skips_settings_it_cannot_parse_and_can_be_turned_off(tmp_path: Path) -> None:
    root = _target(tmp_path)
    settings = root / pa.CLAUDE_SETTINGS_RELPATH
    settings.parent.mkdir()
    settings.write_text("{not json")
    r = pa.install_into_repo("target", root, python="/p")
    assert settings.read_text() == "{not json"
    assert any("not valid JSON" in a for a in r.actions)
    settings.unlink()
    r = pa.install_into_repo("target", root, guard_hook=False)
    assert not settings.exists() and not any("settings.local.json" in a for a in r.actions)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _target(tmp_path)
    r = pa.install_into_repo("target", root, python="/p", dry_run=True)
    assert not (root / pa.CLAUDE_SETTINGS_RELPATH).exists()
    assert not (root / ".git" / "info" / "exclude").exists()
    assert any("would" in a and "settings.local.json" in a for a in r.actions)
