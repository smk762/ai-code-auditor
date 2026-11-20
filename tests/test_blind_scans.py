"""A scan that couldn't review a file (over budget, cache-only miss) says nothing about it.

The capped nights of 2026-11-18..20 ran the nightly pass cache-only over code the batch never
reviewed: those units returned no findings, the repo still counted as scanned, and pack GC closed
packs as not_rescanned on three "silent" scans nobody had looked at the code for.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor import trust_metrics as tm
from auditor.config import ModelProviderConfig
from auditor.contracts import CodeUnit
from auditor.llm_client import LLMClient
from auditor.pack_gc import plan
from tests.test_pack_gc import CODE, NOW, FakeMirror, at, ids, pack


def blind(run: str, hours: float, paths: tuple[str, ...] = ("app/calc.py",)) -> tm.Scan:
    return tm.Scan(run_id=run, repo="x", at=at(hours), findings=[], unreviewed=frozenset(paths))


def seen_scan(run: str, hours: float) -> tm.Scan:
    return tm.Scan(run_id=run, repo="x", at=at(hours), findings=[])


def test_blind_scans_never_close_a_pack_as_not_rescanned() -> None:
    m = {"x": FakeMirror({"app/calc.py": CODE}, {"app/calc.py": at(0)})}
    p = pack("n", seen=1)
    assert ids(plan([p], [blind("s1", 10), blind("s2", 20), blind("s3", 30)], m, now=NOW, misses=3)) == {}
    # Two scans that did read the file plus one that didn't are still not three.
    assert ids(plan([p], [seen_scan("s1", 10), blind("s2", 20), seen_scan("s3", 30)], m, now=NOW, misses=3)) == {}
    # Blind about another file only: this file was read, so the silence counts.
    other = [blind("s1", 10, ("other.py",)), blind("s2", 20, ("other.py",)), blind("s3", 30, ("other.py",))]
    assert ids(plan([p], other, m, now=NOW, misses=3)) == {"n": "not_rescanned"}


def test_blind_scans_dont_confirm_code_changed() -> None:
    m = {"x": FakeMirror({"app/calc.py": ["def add(a, b):", "    return a + b", ""]})}
    p = pack("d", kind="diff_audit", seen=1)
    assert ids(plan([p], [blind("s1", 10)], m, now=NOW)) == {}
    assert ids(plan([p], [seen_scan("s1", 10)], m, now=NOW)) == {"d": "code_changed"}


def test_metrics_ignore_findings_a_blind_scan_could_not_see() -> None:
    f = {"path": "app/calc.py", "title": "add subtracts", "line": 2, "type": "bug"}
    prev = tm.Scan(run_id="a", repo="x", at=at(0), findings=[f])
    cur = blind("b", 10)
    assert tm.flow_metric([prev, cur]) == {}  # not "gone"
    assert tm.scan_persistence_metric([prev, cur], [])["findings"] == 0  # not "not refound"
    back = tm.Scan(run_id="c", repo="x", at=at(20), findings=[f])
    assert all(not w["new"] for w in tm.flow_metric([cur, back]).values())  # not "new" either


def _unit(path: str) -> CodeUnit:
    return CodeUnit(id=path, repo="r", file_path=path, symbol="f", kind="function", language="python",
                    start_line=1, end_line=2, raw_text=f"def f():\n    return '{path}'\n")


def test_client_records_files_it_could_not_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE", "1")
    monkeypatch.setenv("AI_AUDIT_LLM_CACHE_PATH", str(tmp_path / "c.db"))
    cfg = ModelProviderConfig(provider="anthropic", model="claude-haiku-5-5", endpoint="")
    live = LLMClient(provider_config=cfg)
    monkeypatch.setattr(live, "generate", lambda p: '[]')
    live.analyze_code(_unit("/m/r/reviewed.py"))
    monkeypatch.setattr(live, "generate", lambda p: "")  # over budget / backend down
    live.analyze_code(_unit("/m/r/no_answer.py"))
    assert live.unreviewed_files == {"/m/r/no_answer.py"}

    monkeypatch.setenv("AI_AUDIT_LLM_CACHE_ONLY", "1")
    cached = LLMClient(provider_config=cfg)
    cached.analyze_code(_unit("/m/r/reviewed.py"))  # in the cache
    cached.analyze_code(_unit("/m/r/never_reviewed.py"))
    assert cached.unreviewed_files == {"/m/r/never_reviewed.py"}


def test_load_scans_reads_unreviewed_files_from_the_run() -> None:
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from auditor.db import AuditRun, Base, FindingRecord

    now = datetime.now(UTC)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for run, extra in (("r1", {"unreviewed_files": {"x": ["app/calc.py"]}}), ("r2", {})):
            s.add(AuditRun(run_id=run, pipeline="repo-audit", status="success", started_at=now,
                           metadata_json={"scanned_repos": ["x", "y"], "extra": extra}))
            s.add(FindingRecord(run_id=run, repo="y", file_path="/m/y/a.py", line=1, finding_type="bug",
                                severity="LOW", title="t", source="llm", created_at=now))
        s.commit()
        scans = {(sc.run_id, sc.repo): sc for sc in tm.load_scans(s)}
    assert not scans[("r1", "x")].covers("app/calc.py") and scans[("r1", "x")].covers("app/other.py")
    assert scans[("r2", "x")].covers("app/calc.py") and scans[("r1", "y")].covers("app/calc.py")
