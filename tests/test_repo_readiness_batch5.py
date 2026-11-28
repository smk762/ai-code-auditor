"""scripts/repo_readiness.py: same-second runs and failed repos (batch-5 packs). No LLM calls."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from tests.test_repo_readiness_packs import _load

PRE = {"source_files": 1, "opsec": [], "undocumented_env": {}, "env_vars": 0, "undocumented_routes": {},
       "routes": 0, "module_docstring_pct": None, "oversize_in_tree": []}


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 28, 1, 2, 3, tzinfo=tz)


def _stub(mod, monkeypatch, tmp_path: Path, broken: set[str]) -> None:
    repos = [SimpleNamespace(name=n, enabled=True) for n in ("argus-good", "argus-bad")]
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "datetime", _FrozenClock)
    monkeypatch.setattr(mod, "load_ecosystem_config", lambda: SimpleNamespace(repos=repos))

    def snapshot(repo, snapdir):
        if repo.name in broken:
            raise RuntimeError("git archive failed")
        return {"path": tmp_path, "sha": "a" * 40}

    monkeypatch.setattr(mod, "snapshot", snapshot)
    monkeypatch.setattr(mod, "git_info", lambda snap: {})
    monkeypatch.setattr(mod, "prechecks", lambda path: dict(PRE))
    monkeypatch.setattr(mod, "issue_body", lambda *a, **k: "body")
    monkeypatch.setattr(sys, "argv", ["repo_readiness.py", "--snapshot-dir", str(tmp_path / "snaps")])


def test_two_runs_in_the_same_second_get_their_own_run_dirs(tmp_path: Path, monkeypatch) -> None:
    mod = _load(monkeypatch)
    _stub(mod, monkeypatch, tmp_path, broken=set())
    assert mod.main() == 0
    assert mod.main() == 0  # was FileExistsError: same %Y%m%d-%H%M%S name
    runs = sorted(p.name for p in (tmp_path / "reports" / "repo_readiness").iterdir())
    assert len(runs) == 2
    assert all((tmp_path / "reports" / "repo_readiness" / r / "summary.md").is_file() for r in runs)


def test_a_failed_repo_is_an_error_row_and_a_nonzero_exit(tmp_path: Path, monkeypatch, capsys) -> None:
    mod = _load(monkeypatch)
    _stub(mod, monkeypatch, tmp_path, broken={"argus-bad"})
    assert mod.main() == 1  # was 0: the report looked complete
    out = capsys.readouterr().out
    assert "argus-bad: RuntimeError: git archive failed" in out
    [run] = (tmp_path / "reports" / "repo_readiness").iterdir()
    summary = (run / "summary.md").read_text()
    assert "| argus-bad | ERROR: RuntimeError: git archive failed |" in summary
    assert "| argus-good |" in summary
    row = next(line for line in summary.splitlines() if line.startswith("| argus-bad |"))
    header = next(line for line in summary.splitlines() if line.startswith("| Repo |"))
    assert row.count("|") == header.count("|")
