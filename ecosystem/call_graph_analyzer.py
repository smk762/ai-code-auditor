"""
Inter-service call graph analyzer.

Scans Python source files for outbound HTTP client calls (httpx, requests,
aiohttp) and infers which target service is being called by looking for
service URL env-var fragments in:
  - the URL argument passed directly to the HTTP call
  - variables assigned from those env vars and then used as the URL

Detected edges are validated against ``call_graph_rules.allowed_calls`` from
``config/architecture_rules.yaml``.  Any edge not in the allowed list is
returned as a HIGH-severity ``RuleViolation``.

Edges are deduplicated: one violation per (from_repo, to_service) pair, with
all evidence files listed.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from auditor.contracts import RuleViolation

HTTP_CLIENT_MODULES = frozenset({"httpx", "requests", "aiohttp", "urllib"})
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "request", "send", "head", "options"})


def detect_call_graph_violations(
    repo_files: dict[str, list[Path]],
    call_graph_rules: dict,
) -> list[RuleViolation]:
    """
    Return violations for inter-service HTTP calls not in the allowed list.

    Args:
        repo_files: mapping of repo_name → list of source file paths
        call_graph_rules: the ``call_graph_rules`` block from architecture_rules.yaml
    """
    env_to_service: dict[str, str] = {
        item["env_fragment"].upper(): item["service"]
        for item in call_graph_rules.get("service_url_env_vars", [])
        if "env_fragment" in item and "service" in item
    }
    allowed_calls_raw = call_graph_rules.get("allowed_calls", [])
    allowed_set: set[tuple[str, str]] = {
        (item["from"], item["to"])
        for item in allowed_calls_raw
        if "from" in item and "to" in item
    }
    wildcard_senders: set[str] = {
        item["from"] for item in allowed_calls_raw if item.get("to") == "*"
    }

    if not env_to_service:
        return []

    # Accumulate evidence per (from_repo, to_service) to avoid duplicate violations.
    evidence_map: dict[tuple[str, str], list[str]] = defaultdict(list)

    for repo_name, files in repo_files.items():
        if repo_name in wildcard_senders:
            continue
        for file_path in files:
            if file_path.suffix != ".py":
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            edges = _find_service_calls(source, file_path, repo_name, env_to_service)
            for edge in edges:
                to_service = edge["to_service"]
                if (repo_name, to_service) not in allowed_set:
                    key = (repo_name, to_service)
                    evidence_map[key].append(
                        f"{file_path.name}:{edge['line']} ({edge['evidence']})"
                    )

    violations: list[RuleViolation] = []
    for (from_repo, to_service), evidence_items in sorted(evidence_map.items()):
        violations.append(
            RuleViolation(
                rule_name="call_graph_violation",
                violating_path=f"{from_repo} → {to_service}",
                entities=[from_repo, to_service],
                evidence=(
                    f"Unauthorised direct call from '{from_repo}' to '{to_service}'. "
                    f"Locations: {'; '.join(evidence_items[:5])}"
                    + (" (and more)" if len(evidence_items) > 5 else "")
                ),
                severity="HIGH",
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Internal AST scanning helpers
# ---------------------------------------------------------------------------


def _find_service_calls(
    source: str,
    file_path: Path,
    repo_name: str,
    env_to_service: dict[str, str],
) -> list[dict]:
    """
    Return call-edge dicts for any HTTP client call whose URL references a
    recognised service URL env var fragment.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Pass 1: collect variable names that were assigned from a service env var.
    # e.g. gothmog_url = os.getenv("GOTHMOG_URL")  →  {"GOTHMOG_URL": "gothmog", ...}
    service_var_names: dict[str, str] = {}  # upper(var_name) → service
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        try:
            rhs = ast.unparse(node.value).upper()
        except Exception:
            continue
        for frag, svc in env_to_service.items():
            if frag in rhs:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        service_var_names[target.id.upper()] = svc
                break

    # Pass 2: find HTTP client method calls and check URL argument.
    results: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if not isinstance(func, ast.Attribute):
            continue

        method = func.attr.lower()
        if method not in HTTP_METHODS:
            continue

        # Only interested in direct httpx.post / requests.get style calls.
        # (Not self.client.get — that requires flow analysis.)
        caller = func.value
        if not isinstance(caller, ast.Name):
            continue
        if caller.id.lower() not in HTTP_CLIENT_MODULES:
            continue

        url_node = _url_arg(node)
        if url_node is None:
            continue

        svc = _service_from_url_node(url_node, env_to_service, service_var_names)
        if svc:
            results.append(
                {
                    "to_service": svc,
                    "line": node.lineno,
                    "method": method,
                    "evidence": f"{caller.id}.{method}()",
                }
            )

    return results


def _url_arg(call: ast.Call) -> ast.expr | None:
    """Extract the URL argument from an HTTP client call node."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "url":
            return kw.value
    return None


def _service_from_url_node(
    url_node: ast.expr,
    env_to_service: dict[str, str],
    service_var_names: dict[str, str],
) -> str | None:
    """
    Return the target service name if the URL node references a known service,
    or None if it can't be determined.
    """
    # Case 1: URL is a variable name previously assigned from a service env var.
    if isinstance(url_node, ast.Name):
        return service_var_names.get(url_node.id.upper())

    # Case 2: URL is an expression we can unparse and search for fragments.
    try:
        url_repr = ast.unparse(url_node).upper()
    except Exception:
        return None

    for frag, svc in env_to_service.items():
        if frag in url_repr:
            return svc

    return None
