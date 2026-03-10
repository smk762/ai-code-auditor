from pathlib import Path

from qa.benchmark import run_benchmark


def test_run_benchmark_returns_metrics(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("qa/fixtures").mkdir(parents=True, exist_ok=True)
    Path("qa/fixtures/benchmark_queries.yaml").write_text(
        "queries:\n  - query: test\n    expected_repos: []\n",
        encoding="utf-8",
    )
    result = run_benchmark("qa/fixtures/benchmark_queries.yaml")
    assert result.total_queries == 1
    assert 0.0 <= result.precision <= 1.0
    assert 0.0 <= result.recall <= 1.0
