from __future__ import annotations

import pytest

from auditor.contracts import CodeUnit
from patterns import extraction_candidate_builder as ecb


def _unit(uid: str, repo: str, text: str) -> CodeUnit:
    return CodeUnit(id=uid, repo=repo, file_path=f"{repo}/m.py", symbol=uid, kind="function",
                    language="python", start_line=1, end_line=3, raw_text=text)


def test_indented_method_gets_a_fingerprint() -> None:
    method = "    def fetch(self, url):\n        return self.session.get(url)\n"
    assert ecb._normalized_ast_repr(_unit("a", "r1", method)) != ""
    assert ecb._extract_signature(method, "python") == {"kind": "function", "args": 2, "raises": 0}


def test_lsh_miss_on_a_large_corpus_skips_the_all_pairs_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    units = [
        _unit("a", "r1", "def alpha(x):\n    return x + 1\n"),
        _unit("b", "r2", "def beta(items, key):\n    return sorted(items, key=key)\n"),
        _unit("c", "r3", "def gamma():\n    raise RuntimeError('nope')\n"),
    ]
    monkeypatch.setattr(ecb, "_candidate_pairs_from_buckets", lambda buckets: set())
    calls: list[int] = []
    real = ecb._all_cross_repo_pairs
    monkeypatch.setattr(ecb, "_all_cross_repo_pairs", lambda us: calls.append(len(us)) or real(us))

    monkeypatch.setenv("AI_AUDIT_DUP_FALLBACK_MAX", "2")
    assert ecb.detect_duplicate_clusters(units) == []
    assert calls == []

    monkeypatch.setenv("AI_AUDIT_DUP_FALLBACK_MAX", "50")  # small corpus: exhaustive check is cheap
    ecb.detect_duplicate_clusters(units)
    assert calls == [3]
