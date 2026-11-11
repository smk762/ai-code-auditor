"""
Service topology renderer.

Builds a single self-contained HTML artifact (with an inline Graphviz-rendered
SVG) showing services, their interactions, and any violations detected during
the latest ecosystem audit.  Also writes a JSON sidecar with the same data.

Inputs:
    - architecture rules dict (loaded from ``config/architecture_rules.yaml``)
    - observed inter-service edges (from ``analyze_call_graph``)
    - all governance violations for the run
    - the set of enabled (scanned) repos for this run

Outputs (under ``output_dir``):
    - ``service_topology.html`` — single-file artifact, inline SVG
    - ``service_topology.json`` — structured sidecar for downstream tools

Fails soft: any rendering error is logged and ``None`` is returned so the
audit run continues to completion.
"""
from __future__ import annotations

import html
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from auditor.contracts import ObservedCallEdge, RuleViolation

if TYPE_CHECKING:
    from auditor.runtime import RunMetadata

logger = logging.getLogger(__name__)

# Role → fill colour (Graphviz colour names).
_ROLE_COLOURS: dict[str, str] = {
    "gateway": "#9ec6e8",
    "ui": "#d6a3d6",
    "orchestrator": "#f5b86b",
    "worker": "#a6d9a6",
    "monitoring": "#cccccc",
    "tooling": "#e6e6e6",
}
_DEFAULT_ROLE_COLOUR = "#ffffff"

# Violations whose `entities[0]` should be rendered as a node-level badge
# (i.e. governance issues that aren't already drawn as a bad edge).
_NODE_BADGE_RULES: frozenset[str] = frozenset(
    {
        "ui_in_worker",
        "competing_providers",
        "domain_boundary_violation",
        "service_boundary",
        "dependency_direction",
    }
)

_WILDCARD_PHANTOM_ID = "__wildcard_target__"


def write_service_topology(
    rules: dict,
    observed_edges: Iterable[ObservedCallEdge],
    violations: Iterable[RuleViolation],
    enabled_repos: Iterable[str],
    output_dir: str = "reports",
    run_metadata: "RunMetadata | None" = None,
) -> Path | None:
    """
    Render service topology HTML + JSON sidecar.  Returns the HTML path on
    success, or ``None`` if rendering failed (the audit run continues either
    way).
    """
    try:
        return _write(
            rules=rules,
            observed_edges=list(observed_edges),
            violations=list(violations),
            enabled_repos=set(enabled_repos),
            output_dir=output_dir,
            run_metadata=run_metadata,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("Service topology render failed: %s", exc, exc_info=True)
        return None


def _write(
    rules: dict,
    observed_edges: list[ObservedCallEdge],
    violations: list[RuleViolation],
    enabled_repos: set[str],
    output_dir: str,
    run_metadata: "RunMetadata | None",
) -> Path | None:
    service_roles = rules.get("service_roles") or []
    if not service_roles:
        logger.info("Service topology: no service_roles in rules; skipping render.")
        return None

    call_graph_rules = rules.get("call_graph_rules") or {}
    allowed_calls_raw = call_graph_rules.get("allowed_calls") or []

    nodes = _build_nodes(service_roles, enabled_repos, violations)
    node_names = {n["name"] for n in nodes}

    edges = _build_edges(
        allowed_calls_raw=allowed_calls_raw,
        observed_edges=observed_edges,
        node_names=node_names,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    svg = _render_svg(nodes, edges)

    generated_at = datetime.now(timezone.utc).isoformat()
    run_id = run_metadata.run_id if run_metadata else ""
    scanned_repos = sorted(enabled_repos)

    json_path = out / "service_topology.json"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "run_id": run_id,
                "scanned_repos": scanned_repos,
                "nodes": nodes,
                "edges": edges,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    html_path = out / "service_topology.html"
    html_path.write_text(
        _render_html(
            svg=svg,
            generated_at=generated_at,
            run_id=run_id,
            scanned_repos=scanned_repos,
            edge_kinds=[e["kind"] for e in edges],
            node_violation_total=sum(n["violation_count"] for n in nodes),
        ),
        encoding="utf-8",
    )

    logger.info("Service topology written: %s", html_path)
    return html_path


# ---------------------------------------------------------------------------
# Node / edge construction
# ---------------------------------------------------------------------------


def _build_nodes(
    service_roles: list[dict],
    enabled_repos: set[str],
    violations: list[RuleViolation],
) -> list[dict]:
    badge_counts: dict[str, int] = defaultdict(int)
    for v in violations:
        if v.rule_name not in _NODE_BADGE_RULES:
            continue
        if not v.entities:
            continue
        badge_counts[v.entities[0]] += 1

    nodes: list[dict] = []
    for entry in service_roles:
        name = entry.get("name")
        if not name:
            continue
        role = entry.get("role", "")
        nodes.append(
            {
                "name": name,
                "role": role,
                "disabled": name not in enabled_repos,
                "violation_count": badge_counts.get(name, 0),
            }
        )
    return nodes


def _build_edges(
    allowed_calls_raw: list[dict],
    observed_edges: list[ObservedCallEdge],
    node_names: set[str],
) -> list[dict]:
    """
    Returns edges in render order with one of four kinds:
        allowed_observed     — policy permits, source confirms
        allowed_policy_only  — policy permits, no source evidence this scan
        violation_observed   — source shows it, policy forbids
        wildcard             — sender allowed to reach any service ("→ all")
    """
    explicit_allowed: set[tuple[str, str]] = set()
    wildcard_senders: set[str] = set()
    for item in allowed_calls_raw:
        frm, to = item.get("from"), item.get("to")
        if not frm or not to:
            continue
        if to == "*":
            wildcard_senders.add(frm)
        else:
            explicit_allowed.add((frm, to))

    observed_by_pair: dict[tuple[str, str], ObservedCallEdge] = {
        (e.from_repo, e.to_service): e for e in observed_edges
    }

    edges: list[dict] = []

    # Explicit allowed pairs first — observed (solid) or policy-only (dashed).
    for from_repo, to_service in sorted(explicit_allowed):
        seen = observed_by_pair.get((from_repo, to_service))
        if seen is not None:
            edges.append(
                {
                    "from": from_repo,
                    "to": to_service,
                    "kind": "allowed_observed",
                    "evidence": seen.evidence,
                }
            )
        else:
            edges.append(
                {
                    "from": from_repo,
                    "to": to_service,
                    "kind": "allowed_policy_only",
                    "evidence": [],
                }
            )

    # Violations: observed but not allowed, and sender isn't a wildcard sender.
    for edge in sorted(
        observed_edges, key=lambda e: (e.from_repo, e.to_service)
    ):
        if edge.allowed:
            continue
        edges.append(
            {
                "from": edge.from_repo,
                "to": edge.to_service,
                "kind": "violation_observed",
                "evidence": edge.evidence,
            }
        )

    # Wildcard senders → single dashed edge to the phantom "all monitored" node.
    for sender in sorted(wildcard_senders):
        if sender not in node_names:
            continue
        targets = sorted(
            {
                e.to_service
                for e in observed_edges
                if e.from_repo == sender and e.to_service in node_names
            }
        )
        edges.append(
            {
                "from": sender,
                "to": _WILDCARD_PHANTOM_ID,
                "kind": "wildcard",
                "evidence": [],
                "wildcard_targets": targets,
            }
        )

    return edges


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_svg(nodes: list[dict], edges: list[dict]) -> str:
    """Render the topology with Graphviz; return inline SVG (without xml decl)."""
    import graphviz  # local import so module can load without the binary

    g = graphviz.Digraph(
        "service_topology",
        graph_attr={
            "rankdir": "LR",
            "bgcolor": "transparent",
            "fontname": "Helvetica",
            "splines": "spline",
            "pad": "0.4",
        },
        node_attr={
            "fontname": "Helvetica",
            "fontsize": "11",
            "shape": "box",
            "style": "rounded,filled",
        },
        edge_attr={"fontname": "Helvetica", "fontsize": "9"},
    )

    has_wildcard = any(e["kind"] == "wildcard" for e in edges)

    for node in nodes:
        fill = _ROLE_COLOURS.get(node["role"], _DEFAULT_ROLE_COLOUR)
        label = node["name"]
        if node["role"]:
            label = f"{node['name']}\\n<{node['role']}>"
        if node["violation_count"]:
            label += f"\\n⚠ {node['violation_count']}"
        attrs: dict[str, str] = {
            "label": label,
            "fillcolor": fill,
            "color": "#444444",
        }
        if node["disabled"]:
            attrs["style"] = "rounded,filled,dashed"
            attrs["fontcolor"] = "#777777"
            attrs["color"] = "#888888"
        g.node(node["name"], **attrs)

    if has_wildcard:
        g.node(
            _WILDCARD_PHANTOM_ID,
            label="(all monitored\\nservices)",
            shape="ellipse",
            style="dashed,filled",
            fillcolor="#f5f5f5",
            fontcolor="#555555",
            color="#888888",
        )

    for edge in edges:
        kind = edge["kind"]
        if kind == "allowed_observed":
            attrs = {"color": "#1f8a3a", "penwidth": "1.6"}
        elif kind == "allowed_policy_only":
            attrs = {"color": "#888888", "style": "dashed", "penwidth": "1.0"}
        elif kind == "violation_observed":
            count = len(edge.get("evidence") or [])
            attrs = {
                "color": "#c0392b",
                "penwidth": "2.4",
                "label": f"!{count}" if count else "!",
                "fontcolor": "#c0392b",
            }
        elif kind == "wildcard":
            targets = edge.get("wildcard_targets") or []
            label = f"→ all ({len(targets)} seen)" if targets else "→ all"
            attrs = {
                "color": "#888888",
                "style": "dashed",
                "label": label,
                "fontcolor": "#666666",
            }
        else:  # pragma: no cover — defensive
            attrs = {}
        g.edge(edge["from"], edge["to"], **attrs)

    raw = g.pipe(format="svg").decode("utf-8")
    # Strip the leading <?xml ...?> and DOCTYPE so the SVG can sit inside HTML.
    if raw.startswith("<?xml"):
        raw = raw.split("?>", 1)[1].lstrip()
    if raw.startswith("<!DOCTYPE"):
        raw = raw.split(">", 1)[1].lstrip()
    return raw


def _render_html(
    svg: str,
    generated_at: str,
    run_id: str,
    scanned_repos: list[str],
    edge_kinds: list[str],
    node_violation_total: int,
) -> str:
    counts = {
        "allowed_observed": edge_kinds.count("allowed_observed"),
        "allowed_policy_only": edge_kinds.count("allowed_policy_only"),
        "violation_observed": edge_kinds.count("violation_observed"),
        "wildcard": edge_kinds.count("wildcard"),
    }
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Service topology</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif;
          margin: 24px; color: #222; }}
  h1 {{ font-size: 18px; margin: 0 0 4px 0; }}
  .sub {{ color: #555; font-size: 13px; margin-bottom: 16px; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 16px;
             font-size: 12px; margin: 12px 0 20px 0; }}
  .legend .item {{ display: flex; align-items: center; gap: 6px; }}
  .swatch {{ width: 28px; height: 3px; display: inline-block; }}
  .swatch.solid-green {{ background: #1f8a3a; height: 3px; }}
  .swatch.dashed-grey {{ border-top: 2px dashed #888; height: 0; }}
  .swatch.bold-red {{ background: #c0392b; height: 4px; }}
  .role-chip {{ width: 14px; height: 14px; border: 1px solid #444;
                display: inline-block; border-radius: 3px; }}
  .graph {{ overflow: auto; border: 1px solid #eaeaea; padding: 12px;
            border-radius: 6px; background: #fff; }}
  footer {{ margin-top: 18px; color: #666; font-size: 12px; }}
  footer a {{ color: #335; }}
  code {{ background: #f3f3f3; padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>
  <h1>Service topology</h1>
  <div class="sub">
    {counts["allowed_observed"]} allowed-observed · {counts["allowed_policy_only"]} policy-only ·
    {counts["violation_observed"]} violation{"s" if counts["violation_observed"] != 1 else ""} ·
    {counts["wildcard"]} wildcard sender{"s" if counts["wildcard"] != 1 else ""} ·
    {node_violation_total} node badge{"s" if node_violation_total != 1 else ""}
  </div>

  <div class="legend">
    <div class="item"><span class="swatch solid-green"></span> allowed &amp; observed</div>
    <div class="item"><span class="swatch dashed-grey"></span> policy-only (no source evidence)</div>
    <div class="item"><span class="swatch bold-red"></span> violation (observed, not allowed)</div>
    <div class="item"><span class="role-chip" style="background:{_ROLE_COLOURS['gateway']}"></span> gateway</div>
    <div class="item"><span class="role-chip" style="background:{_ROLE_COLOURS['ui']}"></span> ui</div>
    <div class="item"><span class="role-chip" style="background:{_ROLE_COLOURS['orchestrator']}"></span> orchestrator</div>
    <div class="item"><span class="role-chip" style="background:{_ROLE_COLOURS['worker']}"></span> worker</div>
    <div class="item"><span class="role-chip" style="background:{_ROLE_COLOURS['monitoring']}"></span> monitoring</div>
    <div class="item"><span class="role-chip" style="background:{_ROLE_COLOURS['tooling']}"></span> tooling</div>
    <div class="item">⚠ N — governance issues at node</div>
  </div>

  <div class="graph">{svg}</div>

  <footer>
    Generated {html.escape(generated_at)} ·
    run <code>{html.escape(run_id) if run_id else "(no run id)"}</code> ·
    {len(scanned_repos)} repo{"s" if len(scanned_repos) != 1 else ""} scanned ·
    <a href="ecosystem_report.md">ecosystem_report.md</a> ·
    <a href="service_topology.json">JSON sidecar</a>
  </footer>
</body>
</html>
"""
