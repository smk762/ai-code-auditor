from pathlib import Path

from pipelines.ecosystem_audit import run as ecosystem_run
from pipelines.nightly_repo_audit import run as repo_run


def _stub_llm_client(monkeypatch) -> None:
    """Prevent any real network calls to Ollama during smoke tests."""
    import auditor.llm_client as llm_mod
    monkeypatch.setattr(llm_mod.LLMClient, "health_check", lambda self: (True, "ok"))
    monkeypatch.setattr(llm_mod.LLMClient, "generate", lambda self, prompt: "[]")


def test_pipeline_smoke_generates_reports(tmp_path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ecosystem.yaml").write_text(
        f"""
repos:
  - name: test-repo
    path: {repo_root}
output_dir: {tmp_path / "reports"}
graph_db_path: {tmp_path / "graph" / "ecosystem_graph.db"}
embedding_index_path: {tmp_path / "index" / "code_embeddings.faiss"}
architecture_memory_path: {tmp_path / "memory" / "architecture_memory.md"}
""",
        encoding="utf-8",
    )
    (config_dir / "architecture_rules.yaml").write_text("rules: []\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_AUDIT_SKIP_GPU_SCHEDULER", "1")
    _stub_llm_client(monkeypatch)
    repo_run()
    ecosystem_run()

    assert (tmp_path / "reports" / "nightly_repo_report.md").exists()
    assert (tmp_path / "reports" / "security_report.md").exists()
    assert (tmp_path / "reports" / "ecosystem_report.md").exists()
    assert (tmp_path / "reports" / "architecture_violations.md").exists()
    assert (tmp_path / "reports" / "pattern_proposals.md").exists()
    assert (tmp_path / "reports" / "extraction_candidates.yaml").exists()
    assert (tmp_path / "reports" / "extraction_candidates.md").exists()
    assert (tmp_path / "graph" / "ecosystem_graph.db").exists()
    assert (tmp_path / "index" / "code_embeddings.meta.json").exists()
