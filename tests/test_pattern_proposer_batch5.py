"""Extraction candidate ids come from the cluster, not its position (patterns/pattern_proposer.py)."""
from __future__ import annotations

from auditor.contracts import CodeUnit
from patterns.pattern_proposer import propose_extraction_candidates

SIGNALS = {"ast_similarity_score": 0.9, "embedding_similarity_score": 0.9, "signature_compatibility_score": 0.9,
           "similarity_score": 1.0}


def _cluster(symbol: str, repos: list[str], cluster_id: str = "") -> dict:
    units = [CodeUnit(id=f"{r}:{symbol}", repo=r, file_path=f"/m/{r}/{symbol}.py", symbol=symbol, kind="function",
                      language="python", start_line=1, end_line=3) for r in repos]
    c = {"symbol": symbol, "domain": "shared-logic", "repos": repos, "units": units, "signals": SIGNALS}
    if cluster_id:
        c["cluster_id"] = cluster_id
    return c


def _ids(clusters: list[dict]) -> dict[str, str]:
    return {c["title"]: c["id"] for c in propose_extraction_candidates(clusters, None, [], {})}


def test_an_added_skipped_or_reordered_cluster_never_moves_another_clusters_id() -> None:
    a = _cluster("hash", ["r1", "r2", "r3"], cluster_id="cluster-hash-1")
    b = _cluster("now", ["r4", "r5", "r6"])  # no cluster_id: its units decide
    lone = _cluster("solo", ["r1"])  # one repo: skipped
    first = _ids([a, b])
    assert len(set(first.values())) == 2
    assert _ids([b, a]) == first
    assert _ids([lone, _cluster("new", ["r7", "r8", "r9"]), b, a]).items() >= first.items()
