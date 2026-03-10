from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from copilot.copilot_engine import CopilotEngine
from ecosystem.graph_builder import GraphBuilder
from semantic.embedding_index import EmbeddingIndex
from semantic.semantic_search import SemanticSearch


@dataclass(slots=True)
class BenchmarkResult:
    precision: float
    recall: float
    total_queries: int


def run_benchmark(dataset_path: str = "qa/fixtures/benchmark_queries.yaml") -> BenchmarkResult:
    raw = yaml.safe_load(Path(dataset_path).read_text(encoding="utf-8")) or {}
    queries = raw.get("queries", [])

    graph = GraphBuilder("graph/ecosystem_graph.db")
    engine = CopilotEngine(SemanticSearch(EmbeddingIndex("index/code_embeddings.faiss")), graph)
    try:
        tp = 0
        fp = 0
        fn = 0
        for q in queries:
            query = q.get("query", "")
            expected = set(q.get("expected_repos", []))
            response = engine.ask(query)
            predicted = set(response.impacted_repos)
            tp += len(predicted & expected)
            fp += len(predicted - expected)
            fn += len(expected - predicted)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        return BenchmarkResult(precision=precision, recall=recall, total_queries=len(queries))
    finally:
        graph.close()
