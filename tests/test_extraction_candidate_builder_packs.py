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


# ── parse failures, signatures, churn and domains ───────────────────────────

# built at runtime, so pytest parsing this file to report a failure never trips the patched parser
DEEP = "_".join(["too", "deep", "marker"])


def _parse_raising(monkeypatch: pytest.MonkeyPatch, marker: str, exc: Exception) -> None:
    """ast.parse raises *exc* for sources containing *marker* (others, e.g. pytest's own, parse normally)."""
    real = ecb.ast.parse

    def parse(source, *a, **k):
        if isinstance(source, str) and marker in source:
            raise exc
        return real(source, *a, **k)
    monkeypatch.setattr(ecb.ast, "parse", parse)


def test_parse_value_error_skips_the_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Python < 3.12 raises ValueError (not SyntaxError) for NUL bytes in source
    _parse_raising(monkeypatch, "\x00", ValueError("source code string cannot contain null bytes"))
    assert ecb._normalized_ast_repr(_unit("a", "r1", "def f(a, b):\n    return a\x00\n")) == ""
    sig = ecb._extract_signature("def f(a, b):\n    return a\x00\n", "python")
    assert sig == {"kind": "function", "args": 1, "raises": 0}  # the heuristic fallback


def test_deeply_nested_unit_has_no_fingerprint_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    _parse_raising(monkeypatch, DEEP, RecursionError("maximum recursion depth exceeded"))
    src = "def " + DEEP + "():\n    return a.b.c\n"
    assert ecb._normalized_ast_repr(_unit("a", "r1", src)) == ""
    assert ecb._extract_signature(src, "python")["kind"] == "function"


def test_recursion_in_the_canonicaliser_skips_the_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    real = ecb.ast.dump

    def dump(node, *a, **k):
        if any(isinstance(n, ecb.ast.FunctionDef) and n.name == DEEP for n in ecb.ast.walk(node)):
            raise RecursionError("maximum recursion depth exceeded")
        return real(node, *a, **k)
    monkeypatch.setattr(ecb.ast, "dump", dump)
    assert ecb._normalized_ast_repr(_unit("a", "r1", "def " + DEEP + "():\n    return 1\n")) == ""


def test_positional_only_parameters_are_counted() -> None:
    assert ecb._extract_signature("def f(a, b, /, c, *, d):\n    pass\n", "python")["args"] == 4


def test_async_methods_are_counted() -> None:
    cls = "class C:\n    def a(self): pass\n    async def b(self): pass\n    async def c(self): pass\n"
    assert ecb._extract_signature(cls, "python") == {"kind": "class", "args": 3, "raises": 0}


def test_churn_counts_each_file_once(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r1"
    root.mkdir()
    for name in ("a.py", "b.py"):
        (root / name).write_text("x = 1\n")
    units = [CodeUnit(id=f"u{i}", repo="r1", file_path=str(root / f), symbol=f"s{i}", kind="function",
                      language="python", start_line=1, end_line=2, raw_text="pass")
             for i, f in enumerate(["a.py", "a.py", "a.py", "b.py"])]
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ecb, "_count_git_changes", lambda root_, rel, since: calls.append((rel, since)) or 5)
    monkeypatch.setattr(ecb, "_git_contributors", lambda root_, rel, since: {"alice"})
    out = ecb.collect_git_metrics({"units": units}, {"r1": str(root)})
    assert out == {"churn_30d": 10, "churn_90d": 10, "contributors": 1}
    assert sorted(calls) == [("a.py", "30 days"), ("a.py", "90 days"), ("b.py", "30 days"), ("b.py", "90 days")]


@pytest.mark.parametrize("symbol,domain", [
    ("tokenizer", "shared-logic"), ("count_tokens", "shared-logic"), ("author", "shared-logic"),
    ("catalog", "shared-logic"), ("dialog", "shared-logic"), ("topology", "shared-logic"),
    ("exploratory", "shared-logic"), ("fetch_url", "shared-logic"),
    ("login", "authentication"), ("verify_key", "authentication"), ("access_token", "authentication"),
    ("auth_middleware", "authentication"), ("rate_limit", "rate_limiting"), ("RateLimiter", "rate_limiting"),
    ("run_status", "orchestration"), ("observe_job", "orchestration"), ("lora_loader", "model_management"),
    ("with_retry", "resilience"), ("get_logger", "observability"), ("cache_get", "caching"),
    ("http_get", "networking"),
])
def test_domain_is_inferred_from_whole_tokens(symbol: str, domain: str) -> None:
    assert ecb._infer_domain(symbol) == domain
