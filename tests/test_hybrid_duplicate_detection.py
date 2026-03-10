from auditor.contracts import CodeUnit
from patterns.extraction_candidate_builder import detect_duplicate_clusters


def _unit(repo: str, path: str, symbol: str, raw_text: str, uid: str) -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo=repo,
        file_path=path,
        symbol=symbol,
        kind="function",
        language="python",
        start_line=1,
        end_line=max(1, len(raw_text.splitlines())),
        raw_text=raw_text,
        raw_text_hash=uid,
    )


def test_detect_duplicate_clusters_uses_structure_and_semantics() -> None:
    left = _unit(
        repo="repo-a",
        path="/tmp/repo-a/auth.py",
        symbol="verify_token",
        uid="a1",
        raw_text=(
            "def verify_token(token, secret):\n"
            "    claims = decode(token, secret)\n"
            "    if claims.get('exp', 0) < now():\n"
            "        raise ValueError('expired')\n"
            "    return claims\n"
        ),
    )
    right = _unit(
        repo="repo-b",
        path="/tmp/repo-b/security.py",
        symbol="check_jwt",
        uid="b1",
        raw_text=(
            "def check_jwt(jwt_value, signing_key):\n"
            "    data = decode(jwt_value, signing_key)\n"
            "    if data.get('exp', 0) < now():\n"
            "        raise ValueError('expired')\n"
            "    return data\n"
        ),
    )
    unrelated = _unit(
        repo="repo-c",
        path="/tmp/repo-c/math.py",
        symbol="sum_values",
        uid="c1",
        raw_text="def sum_values(values):\n    return sum(values)\n",
    )
    clusters = detect_duplicate_clusters([left, right, unrelated])
    assert clusters, "expected at least one cross-repo duplicate cluster"
    top = clusters[0]
    assert {"repo-a", "repo-b"}.issubset(set(top["repos"]))
    assert top["signals"]["ast_similarity_score"] >= 0.72
    assert top["signals"]["embedding_similarity_score"] >= 0.78
    assert top["signals"]["signature_compatibility_score"] >= 0.60
