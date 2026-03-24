from __future__ import annotations

import ast
from collections import defaultdict
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable

from auditor.contracts import CodeUnit


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def detect_duplicate_clusters(units: list[CodeUnit]) -> list[dict]:
    ast_min = _float_env("AI_AUDIT_DUP_AST_MIN", 0.72)
    embed_min = _float_env("AI_AUDIT_DUP_EMBED_MIN", 0.78)
    sig_min = _float_env("AI_AUDIT_DUP_SIG_MIN", 0.60)
    lsh_bands = _int_env("AI_AUDIT_DUP_LSH_BANDS", 8)
    lsh_rows = _int_env("AI_AUDIT_DUP_LSH_ROWS", 8)

    candidates = [u for u in units if u.kind in {"function", "class"} and u.raw_text.strip()]
    if len(candidates) < 2:
        return []

    # Stage 1: LSH prefilter over token shingles.
    signatures = {u.id: _minhash_signature(_shingles(_tokenize(u.raw_text), 3)) for u in candidates}
    buckets = _lsh_buckets(signatures, bands=max(1, lsh_bands), rows=max(1, lsh_rows))
    pair_ids = _candidate_pairs_from_buckets(buckets)
    if not pair_ids:
        pair_ids = _all_cross_repo_pairs(candidates)

    # Stage 2: AST + embedding + signature compatibility filtering.
    adjacency: dict[str, set[str]] = defaultdict(set)
    by_id = {u.id: u for u in candidates}
    for left_id, right_id in pair_ids:
        left = by_id.get(left_id)
        right = by_id.get(right_id)
        if left is None or right is None or left.repo == right.repo:
            continue
        ast_sim = _ast_similarity(left, right)
        embed_sim = _embedding_similarity(left, right)
        sig_sim = _signature_compatibility(left, right)
        if ast_sim >= ast_min and embed_sim >= embed_min and sig_sim >= sig_min:
            adjacency[left_id].add(right_id)
            adjacency[right_id].add(left_id)

    components = _connected_components(adjacency)
    clusters: list[dict] = []
    for comp in components:
        grouped = [by_id[node_id] for node_id in comp if node_id in by_id]
        repos = sorted({u.repo for u in grouped})
        if len(repos) < 2 or len(grouped) < 2:
            continue
        symbol = _dominant_symbol(grouped)
        signals = _cluster_similarity_signals(grouped)
        clusters.append(
            {
                "cluster_id": f"cluster-{symbol}-{hashlib.sha1(''.join(sorted(comp)).encode('utf-8')).hexdigest()[:8]}",
                "domain": _infer_domain(symbol),
                "symbol": symbol,
                "units": grouped,
                "repos": repos,
                "signals": signals,
            }
        )
    return clusters


def _dominant_symbol(grouped: list[CodeUnit]) -> str:
    by_symbol: dict[str, int] = defaultdict(int)
    for unit in grouped:
        by_symbol[unit.symbol.lower()] += 1
    ranked = sorted(by_symbol.items(), key=lambda item: item[1], reverse=True)
    return ranked[0][0] if ranked else "shared-logic"


def collect_git_metrics(cluster: dict, repo_roots: dict[str, str]) -> dict:
    units = cluster.get("units", [])
    if not units:
        return {"churn_30d": 0, "churn_90d": 0, "contributors": 0}

    changes_30 = 0
    changes_90 = 0
    contributors: set[str] = set()
    for unit in units[:20]:
        repo_root = repo_roots.get(unit.repo)
        if not repo_root:
            continue
        rel_path = _relative_to_repo(unit.file_path, repo_root)
        if not rel_path:
            continue
        changes_30 += _count_git_changes(repo_root, rel_path, "30 days")
        changes_90 += _count_git_changes(repo_root, rel_path, "90 days")
        contributors |= _git_contributors(repo_root, rel_path, "90 days")

    return {
        "churn_30d": changes_30,
        "churn_90d": changes_90,
        "contributors": len(contributors),
    }


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_]\w+|\d+|==|!=|<=|>=|[-+*/%(){}\[\].,:]", text.lower())


def _shingles(tokens: list[str], size: int) -> set[str]:
    if len(tokens) < size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + size]) for i in range(0, len(tokens) - size + 1)}


def _stable_hash(seed: int, token: str) -> int:
    digest = hashlib.sha1(f"{seed}:{token}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _minhash_signature(shingles: set[str], num_hashes: int = 64) -> list[int]:
    if not shingles:
        return [0] * num_hashes
    sig = []
    for seed in range(num_hashes):
        sig.append(min(_stable_hash(seed, sh) for sh in shingles))
    return sig


def _lsh_buckets(signatures: dict[str, list[int]], bands: int = 8, rows: int = 8) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for unit_id, sig in signatures.items():
        for band in range(bands):
            start = band * rows
            end = start + rows
            chunk = tuple(sig[start:end])
            key = f"{band}:{hash(chunk)}"
            buckets[key].append(unit_id)
    return buckets


def _candidate_pairs_from_buckets(buckets: dict[str, list[str]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for unit_ids in buckets.values():
        if len(unit_ids) < 2:
            continue
        unique = sorted(set(unit_ids))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                pairs.add((unique[i], unique[j]))
    return pairs


def _all_cross_repo_pairs(units: list[CodeUnit]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    ordered = sorted(units, key=lambda u: u.id)
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if ordered[i].repo == ordered[j].repo:
                continue
            pairs.add((ordered[i].id, ordered[j].id))
    return pairs


def _connected_components(adjacency: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in adjacency:
        if start in seen:
            continue
        stack = [start]
        comp: list[str] = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.append(node)
            stack.extend(adjacency.get(node, set()) - seen)
        if comp:
            components.append(comp)
    return components


def _ast_similarity(left: CodeUnit, right: CodeUnit) -> float:
    left_repr = _normalized_ast_repr(left)
    right_repr = _normalized_ast_repr(right)
    if not left_repr or not right_repr:
        return _token_jaccard(left.raw_text, right.raw_text)
    return _token_jaccard(left_repr, right_repr)


def _normalized_ast_repr(unit: CodeUnit) -> str:
    if unit.language != "python":
        return ""
    try:
        tree = ast.parse(unit.raw_text)
    except SyntaxError:
        return ""

    class Canonicalizer(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name):  # noqa: N802
            return ast.copy_location(ast.Name(id="VAR", ctx=node.ctx), node)

        def visit_Constant(self, node: ast.Constant):  # noqa: N802
            return ast.copy_location(ast.Constant(value="CONST"), node)

        def visit_arg(self, node: ast.arg):  # noqa: N802
            return ast.copy_location(ast.arg(arg="ARG", annotation=None), node)

    normalized = Canonicalizer().visit(tree)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, include_attributes=False)


def _embedding_similarity(left: CodeUnit, right: CodeUnit) -> float:
    left_vec = _hash_embedding_vec(left.raw_text, 64)
    right_vec = _hash_embedding_vec(right.raw_text, 64)
    dot = sum(a * b for a, b in zip(left_vec, right_vec))
    left_norm = sum(a * a for a in left_vec) ** 0.5 or 1.0
    right_norm = sum(b * b for b in right_vec) ** 0.5 or 1.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _hash_embedding_vec(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    tokens = _tokenize(text)
    if not tokens:
        return vec
    for token in tokens:
        idx = int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:8], 16) % dim
        vec[idx] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _signature_compatibility(left: CodeUnit, right: CodeUnit) -> float:
    left_sig = _extract_signature(left.raw_text, left.language)
    right_sig = _extract_signature(right.raw_text, right.language)
    if left_sig["kind"] != right_sig["kind"]:
        return 0.0
    args_sim = 1.0 - min(1.0, abs(left_sig["args"] - right_sig["args"]) / 6.0)
    raises_sim = 1.0 - min(1.0, abs(left_sig["raises"] - right_sig["raises"]) / 4.0)
    return max(0.0, min(1.0, 0.7 * args_sim + 0.3 * raises_sim))


def _extract_signature(text: str, language: str) -> dict:
    if language == "python":
        try:
            tree = ast.parse(text)
            node = tree.body[0] if tree.body else None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arg_count = len(node.args.args) + len(node.args.kwonlyargs)
                raise_count = sum(1 for n in ast.walk(node) if isinstance(n, ast.Raise))
                return {"kind": "function", "args": arg_count, "raises": raise_count}
            if isinstance(node, ast.ClassDef):
                methods = [n for n in node.body if isinstance(n, ast.FunctionDef)]
                return {"kind": "class", "args": len(methods), "raises": 0}
        except SyntaxError:
            pass
    # fallback heuristic
    args = text.count(",")
    raises = len(re.findall(r"\braise\b|\bthrow\b", text))
    kind = "class" if "class " in text else "function"
    return {"kind": kind, "args": min(args, 20), "raises": min(raises, 10)}


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = set(_tokenize(left))
    right_tokens = set(_tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    inter = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return inter / union if union else 0.0


def _cluster_similarity_signals(units: list[CodeUnit]) -> dict[str, float]:
    ast_scores: list[float] = []
    embed_scores: list[float] = []
    sig_scores: list[float] = []
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            if units[i].repo == units[j].repo:
                continue
            ast_scores.append(_ast_similarity(units[i], units[j]))
            embed_scores.append(_embedding_similarity(units[i], units[j]))
            sig_scores.append(_signature_compatibility(units[i], units[j]))

    return {
        "ast_similarity_score": _avg(ast_scores),
        "embedding_similarity_score": _avg(embed_scores),
        "signature_compatibility_score": _avg(sig_scores),
        "similarity_score": _avg(ast_scores + embed_scores + sig_scores),
    }


def _avg(values: Iterable[float]) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _count_git_changes(repo_root: str, file_path: str, since: str) -> int:
    cmd = ["git", "-C", repo_root, "log", f"--since={since}", "--pretty=format:%H", "--", file_path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _git_contributors(repo_root: str, file_path: str, since: str) -> set[str]:
    cmd = ["git", "-C", repo_root, "log", f"--since={since}", "--pretty=format:%an", "--", file_path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _relative_to_repo(file_path: str, repo_root: str) -> str:
    try:
        return str(Path(file_path).resolve().relative_to(Path(repo_root).resolve()))
    except ValueError:
        return ""


def _infer_domain(symbol: str) -> str:
    keywords = {
        # authentication / access control
        "verify_key": "authentication",
        "check_key": "authentication",
        "auth": "authentication",
        "token": "authentication",
        # rate limiting
        "rate_limit": "rate_limiting",
        "ratelimit": "rate_limiting",
        "throttle": "rate_limiting",
        # orchestration / job lifecycle
        "runstatus": "orchestration",
        "run_status": "orchestration",
        "orchestrat": "orchestration",
        "job_status": "orchestration",
        "observe_job": "orchestration",
        "taskstatus": "orchestration",
        # model / adapter management
        "adapter": "model_management",
        "lora": "model_management",
        "civitai": "model_management",
        "checkpoint": "model_management",
        "sidecar": "model_management",
        "hf_kind": "model_management",
        "hf_checkpoint": "model_management",
        # observability
        "retry": "resilience",
        "log": "observability",
        "metric": "observability",
        "observe": "observability",
        # infrastructure
        "cache": "caching",
        "http": "networking",
    }
    s = symbol.lower()
    for key, domain in keywords.items():
        if key in s:
            return domain
    return "shared-logic"


