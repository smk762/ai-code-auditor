from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from auditor import pack_savings
from auditor.agent_pack import build_pack_from_finding, rebuild_index, write_pack
from auditor.audit_api import app
from auditor.contracts import Finding


def _demo(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    """One repo with a big cited file, orientation docs and one specialist pack."""
    out = tmp_path / "reports"
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    big = repo / "big.py"
    big.write_text("def boom():\n    return 1\n" + "# filler line for size\n" * 2000, encoding="utf-8")
    other = repo / "other.py"
    other.write_text("x = 1\n" * 500, encoding="utf-8")
    (repo / "README.md").write_text("readme " * 100, encoding="utf-8")
    (repo / "SHARED.md").write_text("s" * 400, encoding="utf-8")
    (repo / "CLAUDE.md").write_text("c" * 800 + "\n@SHARED.md\n@../missing/SUITE.md\n", encoding="utf-8")
    finding = Finding(
        id="", type="security", severity="HIGH", repo="demo", file_path="big.py", line=1,
        title="Example issue", description="desc", evidence="ev", recommendation="fix it", source="test",
    )
    write_pack(build_pack_from_finding(finding, repo_path=repo, kind="security", run_id="r1"), out)
    rebuild_index(out)
    return out, repo, [big, other]


def test_orientation_counts_resolved_imports_and_lists_broken(tmp_path: Path):
    _, repo, _ = _demo(tmp_path)
    total, broken = pack_savings.orientation(repo)
    claude = len((repo / "CLAUDE.md").read_text()) // 4
    assert total == claude + 100  # SHARED.md is 400 chars
    assert broken == ["../missing/SUITE.md"]


def test_refresh_writes_report_and_appends_history(tmp_path: Path):
    out, repo, files = _demo(tmp_path)
    eco = tmp_path / "ecosystem.md"
    eco.write_text("e" * 4000, encoding="utf-8")

    report = pack_savings.refresh(output_dir=out, repos=[("demo", repo, files)], ecosystem_md=eco)
    row = report["repos"][0]
    assert row["specialists_in_bundle"] == 1
    assert 0 < row["bundle_tokens"] < row["targeted_tokens"] < row["source_tokens"] + 2000
    assert row["targeted_tokens"] == (
        pack_savings.file_tokens(repo / "big.py") + pack_savings.file_tokens(repo / "README.md") + 1000
    )
    assert 0 < row["saving_vs_targeted"] < 1
    assert report["summary"]["repos_with_broken_imports"] == ["demo"]
    assert report["per_pack"]["packs"] == 1
    assert report["per_pack"]["median_file_to_brief"] > 1
    assert pack_savings.load_report(out)["generated_at"] == report["generated_at"]

    pack_savings.refresh(output_dir=out, repos=[("demo", repo, files)], ecosystem_md=eco)
    history = pack_savings.load_history(out)
    assert len(history) == 2
    assert history[-1]["session_saving"] == report["summary"]["session_saving"]


def test_refresh_quietly_respects_toggle_and_swallows_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_AUDIT_PACK_SAVINGS", "0")
    assert "skipped" in pack_savings.refresh_quietly(tmp_path)
    monkeypatch.setenv("AI_AUDIT_PACK_SAVINGS", "1")
    monkeypatch.setattr(pack_savings, "refresh", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pack_savings.refresh_quietly(tmp_path) == {"error": "boom"}


def test_api_serves_report_and_history(tmp_path: Path, monkeypatch):
    out, repo, files = _demo(tmp_path)
    monkeypatch.setattr("auditor.audit_api._output_dir", lambda: str(out))
    client = TestClient(app)
    assert client.get("/audit/pack-savings.json").status_code == 404

    pack_savings.refresh(output_dir=out, repos=[("demo", repo, files)])
    body = client.get("/audit/pack-savings.json").json()
    assert body["repos"][0]["repo"] == "demo"
    assert len(client.get("/audit/pack-savings.json?history=1").json()["history"]) == 1
