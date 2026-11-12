"""Agent coding packs — compressed, discoverable work items from scans.

Packs live under ``reports/agent_packs/`` with an ``INDEX.json`` for lookup.
Agents discover them via ``GET /audit/packs`` (list/search) and
``GET /audit/packs/{id}`` without needing a pasted brief.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from auditor.contracts import Finding, RuleViolation, utc_now_iso
from auditor.validator import suggest_test_commands

logger = logging.getLogger(__name__)

PACK_VERSION = 1
_SNIPPET_RADIUS = 12
_SEVERITY_SCORE = {"CRITICAL": 1.0, "HIGH": 0.85, "MEDIUM": 0.55, "LOW": 0.3, "INFO": 0.1}

PACK_STATUSES = ("open", "claimed", "in_progress", "resolved", "wontfix")
DEFAULT_PACK_STATUS = "open"
# Statuses a consuming agent should still pick up (resolved/wontfix are closed).
ACTIVE_PACK_STATUSES = ("open", "claimed", "in_progress")
_STATUS_HISTORY_MAX = 20

# Bundle sizing: token_budget is approximated as chars / CHARS_PER_TOKEN.
CHARS_PER_TOKEN = 4
DEFAULT_BUNDLE_TOKEN_BUDGET = 2500
WORLD_ECOSYSTEM_ID = "pack-world-ecosystem"
WORLD_MEMORY_ID = "pack-world-architecture-memory"
_RAG_BATCH_SIZE = 80
_TRUNCATION_MARK = "\n…[truncated]"

PACK_DISCOVERY: dict[str, str] = {
    "list": "/audit/packs",
    "bundle": "/audit/packs/bundle?repo={repo}",
    "world_model": "/audit/packs?kind=world_model",
    "specialist": "/audit/packs?layer=specialist",
    "get": "/audit/packs/{id}",
    "brief": "/audit/packs/{id}/brief",
    "status": "/audit/packs/{id}/status",
    "index_file": "agent_packs/INDEX.json",
}


@dataclass
class AgentCodingPack:
    """Compressed context pack for an external coding agent."""

    work_item_id: str
    kind: str
    priority: dict[str, Any]
    repo: dict[str, Any]
    location: dict[str, Any]
    narrative: dict[str, Any]
    impact: dict[str, Any] = field(default_factory=dict)
    governance: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    repair: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    pack_version: int = PACK_VERSION
    status: str = DEFAULT_PACK_STATUS
    status_note: str = ""
    status_updated_at: str = ""
    status_history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pack_version"] = self.pack_version
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, fallback_id: str = "") -> "AgentCodingPack":
        """Rebuild a pack from its YAML/JSON dict (tolerates legacy packs without status)."""
        status = str(data.get("status") or DEFAULT_PACK_STATUS)
        if status not in PACK_STATUSES:
            status = DEFAULT_PACK_STATUS
        history = data.get("status_history")
        return cls(
            work_item_id=str(data.get("work_item_id") or fallback_id),
            kind=str(data.get("kind") or "finding"),
            priority=dict(data.get("priority") or {}),
            repo=dict(data.get("repo") or {}),
            location=dict(data.get("location") or {}),
            narrative=dict(data.get("narrative") or {}),
            impact=dict(data.get("impact") or {}),
            governance=dict(data.get("governance") or {}),
            verification=dict(data.get("verification") or {}),
            repair=dict(data.get("repair") or {}),
            provenance=dict(data.get("provenance") or {}),
            pack_version=int(data.get("pack_version") or PACK_VERSION),
            status=status,
            status_note=str(data.get("status_note") or ""),
            status_updated_at=str(data.get("status_updated_at") or ""),
            status_history=(
                [h for h in history if isinstance(h, dict)] if isinstance(history, list) else []
            ),
        )

    def apply_status(self, status: str, note: str = "") -> None:
        """Set lifecycle status in place and append a bounded history entry."""
        if status not in PACK_STATUSES:
            raise ValueError(f"invalid pack status {status!r}; expected one of {PACK_STATUSES}")
        now = utc_now_iso()
        self.status = status
        self.status_note = note
        self.status_updated_at = now
        self.status_history = [
            *self.status_history,
            {"status": status, "note": note, "at": now},
        ][-_STATUS_HISTORY_MAX:]

    def layer(self) -> str:
        """world_model = compressed topology/memory; specialist = actionable coding work."""
        return "world_model" if self.kind == "world_model" else "specialist"

    def to_index_entry(self, *, path: str, url: str = "") -> dict[str, Any]:
        return {
            "id": self.work_item_id,
            "kind": self.kind,
            "layer": self.layer(),
            "severity": (self.priority or {}).get("severity", "MEDIUM"),
            "score": (self.priority or {}).get("score", 0.0),
            "status": self.status or DEFAULT_PACK_STATUS,
            "repo": (self.repo or {}).get("name", ""),
            "impacted_repos": list((self.impact or {}).get("impacted_repos") or []),
            "title": (self.narrative or {}).get("title", ""),
            "file_path": (self.location or {}).get("file_path", ""),
            "run_id": (self.provenance or {}).get("run_id", ""),
            "path": path,
            "url": url,
            "detected_at": (self.provenance or {}).get("detected_at", ""),
        }

    def to_brief_md(self) -> str:
        sev = (self.priority or {}).get("severity", "MEDIUM")
        title = (self.narrative or {}).get("title", self.work_item_id)
        repo = (self.repo or {}).get("name", "")
        loc = self.location or {}
        lines = [
            f"# Agent Work Pack: {title}",
            "",
            f"- **id:** `{self.work_item_id}`",
            f"- **kind:** {self.kind}",
            f"- **severity:** {sev}",
            f"- **status:** {self.status or DEFAULT_PACK_STATUS}",
            f"- **repo:** {repo}",
            f"- **location:** `{loc.get('file_path', '')}`:{loc.get('line', 0)}"
            + (f" (`{loc.get('symbol')}`)" if loc.get("symbol") else ""),
            "",
            "## Objective",
            (self.narrative or {}).get("description")
            or (self.narrative or {}).get("recommendation")
            or title,
            "",
            "## Recommendation",
            (self.narrative or {}).get("recommendation") or "(none)",
            "",
            "## Acceptance criteria",
        ]
        criteria = (self.narrative or {}).get("acceptance_criteria") or []
        lines.extend([f"- {c}" for c in criteria] or ["- Fix the finding without regressing related paths."])
        lines.append("")
        lines.append("## Evidence")
        evidence = loc.get("evidence") or (self.narrative or {}).get("description") or ""
        if evidence:
            lines.append("```")
            lines.append(str(evidence)[:2000])
            lines.append("```")
        snippet = loc.get("code_snippet") or ""
        if snippet:
            lines.extend(["", "## Code snippet", "```", snippet[:3000], "```"])
        impact = self.impact or {}
        call_path = impact.get("call_path") or []
        if call_path:
            lines.extend(["", "## Call path", *[f"- {p}" for p in call_path]])
        verify = self.verification or {}
        cmds = verify.get("suggested_test_commands") or []
        if cmds:
            lines.extend(["", "## Verify", *[f"- `{c}`" for c in cmds]])
        rules = (self.governance or {}).get("architecture_rules_hit") or []
        if rules:
            lines.append("")
            lines.append("## Architecture constraints")
            for rule in rules:
                if isinstance(rule, dict):
                    lines.append(f"- {rule.get('rule_name', rule)}: {rule.get('evidence', '')}")
                else:
                    lines.append(f"- {rule}")
        pack_url = (self.repair or {}).get("pack_url") or ""
        if pack_url:
            lines.extend(["", f"Full pack: {pack_url}"])
        lines.append("")
        lines.append("Do not auto-merge. Prefer minimal patches with citations.")
        return "\n".join(lines)

    def to_compact_md(self) -> str:
        """Short brief (no snippet/evidence) used by bundles under a token budget."""
        title = (self.narrative or {}).get("title", self.work_item_id)
        loc = self.location or {}
        lines = [
            f"## {title}",
            f"`{self.work_item_id}` · {self.kind} · "
            f"{(self.priority or {}).get('severity', 'MEDIUM')} · status={self.status or DEFAULT_PACK_STATUS}",
            f"repo: {(self.repo or {}).get('name', '')} · "
            f"`{loc.get('file_path', '')}`:{loc.get('line', 0)}",
            "",
            (self.narrative or {}).get("recommendation") or (self.narrative or {}).get("description") or title,
        ]
        criteria = (self.narrative or {}).get("acceptance_criteria") or []
        if criteria:
            lines.extend(["", "Acceptance:", *[f"- {c}" for c in criteria[:4]]])
        cmds = (self.verification or {}).get("suggested_test_commands") or []
        if cmds:
            lines.extend(["", "Verify: " + "; ".join(f"`{c}`" for c in cmds[:2])])
        return "\n".join(lines)

    def to_rag_text(self) -> str:
        """Slim text for audit_docs retrieval (not a full dump)."""
        entry = self.to_index_entry(path="")
        parts = [
            f"Agent coding pack {self.work_item_id}",
            f"kind={self.kind} layer={entry['layer']} status={entry['status']} "
            f"severity={entry['severity']} repo={entry['repo']}",
            f"title={entry['title']}",
            f"file={entry['file_path']}",
            (self.narrative or {}).get("recommendation") or "",
            f"Fetch full pack via GET /audit/packs/{self.work_item_id}",
        ]
        return "\n".join(p for p in parts if p)


def packs_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / "agent_packs"


def index_path(output_dir: str | Path) -> Path:
    return packs_dir(output_dir) / "INDEX.json"


def pack_file_path(output_dir: str | Path, pack_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", pack_id).strip("-")[:120]
    return packs_dir(output_dir) / f"{safe}.yaml"


def stable_pack_id(
    *,
    repo: str,
    file_path: str,
    title: str,
    source: str = "",
    run_id: str = "",
    finding_id: str = "",
) -> str:
    if finding_id and re.fullmatch(r"[a-zA-Z0-9._-]{4,120}", finding_id):
        return finding_id if finding_id.startswith("pack-") else f"pack-{finding_id}"
    digest = hashlib.sha1(
        f"{repo}|{file_path}|{title}|{source}|{run_id}".encode()
    ).hexdigest()[:10]
    repo_slug = re.sub(r"[^a-z0-9]+", "-", (repo or "repo").lower()).strip("-")[:32]
    return f"pack-{repo_slug}-{digest}"


def read_code_snippet(
    repo_path: Path,
    file_path: str,
    line: int,
    radius: int = _SNIPPET_RADIUS,
) -> str:
    if not file_path:
        return ""
    path = Path(file_path)
    if not path.is_absolute():
        path = repo_path / file_path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    idx = max(1, int(line or 1)) - 1
    start = max(0, idx - radius)
    end = min(len(lines), idx + radius + 1)
    numbered = [f"{i + 1:>5}| {lines[i]}" for i in range(start, end)]
    return "\n".join(numbered)


def _normalize_severity(raw: str) -> str:
    sev = (raw or "MEDIUM").upper()
    if sev not in _SEVERITY_SCORE:
        return "MEDIUM"
    return sev


def _acceptance_from_finding(title: str, recommendation: str, file_path: str) -> list[str]:
    items = [
        f"Address: {title}" if title else "Address the reported finding",
        "Keep the change scoped to the cited file/symbols unless impact requires otherwise",
    ]
    if recommendation:
        items.append(recommendation[:240])
    if file_path:
        items.append(f"Re-check `{file_path}` after the fix")
    return items


def build_pack_from_finding(
    finding: Finding,
    *,
    repo_path: str | Path = "",
    kind: str = "",
    run_id: str = "",
    task_id: str = "",
    call_path: list[str] | None = None,
    directly_changed: list[str] | None = None,
    transitively_affected: list[str] | None = None,
    architecture_rules_hit: list[dict[str, Any]] | None = None,
    draft_pr_url: str | None = None,
    handoff_path: str = "",
    public_base_url: str = "",
    missing_evidence: str = "",
) -> AgentCodingPack:
    root = Path(repo_path) if repo_path else Path(".")
    sev = _normalize_severity(finding.severity)
    pack_id = stable_pack_id(
        repo=finding.repo,
        file_path=finding.file_path,
        title=finding.title,
        source=finding.source,
        run_id=run_id,
        finding_id=finding.id,
    )
    tool, cmds = suggest_test_commands(root, finding.file_path) if root.exists() else ("skipped", [])
    snippet = read_code_snippet(root, finding.file_path, finding.line) if root.exists() else ""
    pack_url = f"{public_base_url.rstrip('/')}/audit/packs/{pack_id}" if public_base_url else ""
    return AgentCodingPack(
        work_item_id=pack_id,
        kind=kind or finding.type or finding.source or "finding",
        priority={"severity": sev, "score": _SEVERITY_SCORE[sev]},
        repo={
            "name": finding.repo,
            "path": str(root) if repo_path else "",
            "base_branch": "dev",
        },
        location={
            "file_path": finding.file_path,
            "line": finding.line,
            "symbol": "",
            "code_snippet": snippet,
            "evidence": finding.evidence,
        },
        narrative={
            "title": finding.title,
            "description": finding.description,
            "recommendation": finding.recommendation,
            "acceptance_criteria": _acceptance_from_finding(
                finding.title, finding.recommendation, finding.file_path
            ),
            "missing_evidence": missing_evidence,
        },
        impact={
            "directly_changed": list(directly_changed or []),
            "transitively_affected": list(transitively_affected or []),
            "call_path": list(call_path or []),
            "impacted_repos": [finding.repo] if finding.repo else [],
        },
        governance={"architecture_rules_hit": list(architecture_rules_hit or []), "related_findings": []},
        verification={
            "validate_endpoint": "POST /audit/validate_patch",
            "suggested_test_commands": cmds,
            "tool": tool,
            "max_iterations": 3,
        },
        repair={
            "slug": pack_id.replace("pack-", "repair/", 1) if pack_id.startswith("pack-") else f"repair/{pack_id}",
            "draft_pr_url": draft_pr_url,
            "pack_url": pack_url,
            "intent_markdown": "",
        },
        provenance={
            "source": finding.source,
            "run_id": run_id,
            "task_id": task_id,
            "detected_at": finding.detected_at or utc_now_iso(),
            "handoff_path": handoff_path,
        },
    )


def build_pack_from_worker_finding(
    finding: dict[str, Any],
    *,
    repo: str,
    repo_path: str | Path,
    mission: str,
    run_id: str,
    task_id: str,
    worker_summary: str = "",
    draft_pr_url: str | None = None,
    handoff_path: str = "",
    public_base_url: str = "",
) -> AgentCodingPack:
    file_path = str(finding.get("file") or finding.get("file_path") or "")
    line = int(finding.get("line") or 0)
    title = str(finding.get("title") or f"{mission} finding")
    sev = _normalize_severity(str(finding.get("severity") or "MEDIUM"))
    description = str(finding.get("missing_evidence") or worker_summary or title)
    recommendation = str(finding.get("recommendation") or f"Fix {title} in {file_path or repo}")
    fid = str(finding.get("id") or "")
    finding_obj = Finding(
        id=fid or "",
        type=str(finding.get("category") or mission or "finding"),
        severity=sev,
        repo=repo,
        file_path=file_path,
        line=line,
        title=title,
        description=description,
        evidence=json.dumps(finding, ensure_ascii=False)[:2000],
        recommendation=recommendation,
        source=f"conductor-{mission}",
    )
    pack = build_pack_from_finding(
        finding_obj,
        repo_path=repo_path,
        kind=mission,
        run_id=run_id,
        task_id=task_id,
        call_path=[str(p) for p in (finding.get("call_path") or [])],
        draft_pr_url=draft_pr_url,
        handoff_path=handoff_path,
        public_base_url=public_base_url,
        missing_evidence=str(finding.get("missing_evidence") or ""),
    )
    pack.location["symbol"] = str(finding.get("symbol") or "")
    pack.repair["intent_markdown"] = pack.to_brief_md()
    return pack


def _read_pack_yaml(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _carry_over_lifecycle(pack: AgentCodingPack, existing: dict[str, Any]) -> None:
    """Re-emitting a pack must not reset an agent-set status back to ``open``."""
    prev = AgentCodingPack.from_dict(existing, fallback_id=pack.work_item_id)
    if prev.status == DEFAULT_PACK_STATUS and not prev.status_history:
        return
    if pack.status != DEFAULT_PACK_STATUS:
        return  # caller set an explicit status (e.g. conductor in_progress)
    pack.status = prev.status
    pack.status_note = prev.status_note
    pack.status_updated_at = prev.status_updated_at
    pack.status_history = prev.status_history
    if not pack.repair.get("draft_pr_url") and prev.repair.get("draft_pr_url"):
        pack.repair["draft_pr_url"] = prev.repair["draft_pr_url"]


def write_pack(
    pack: AgentCodingPack,
    output_dir: str | Path,
    *,
    preserve_status: bool = True,
) -> Path:
    root = packs_dir(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = pack_file_path(output_dir, pack.work_item_id)
    if preserve_status and path.exists():
        existing = _read_pack_yaml(path)
        if existing:
            _carry_over_lifecycle(pack, existing)
    if not pack.repair.get("intent_markdown"):
        pack.repair["intent_markdown"] = pack.to_brief_md()
    path.write_text(
        yaml.safe_dump(pack.to_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # Sidecar brief for agents that prefer markdown
    brief = path.with_suffix(".md")
    brief.write_text(pack.to_brief_md(), encoding="utf-8")
    return path


def rebuild_index(output_dir: str | Path, *, public_base_url: str = "") -> dict[str, Any]:
    root = packs_dir(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for yaml_path in sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("skip bad pack %s: %s", yaml_path, exc)
            continue
        if not isinstance(data, dict) or not data.get("work_item_id"):
            continue
        pack = AgentCodingPack.from_dict(data)
        rel = str(yaml_path.relative_to(Path(output_dir)))
        url = ""
        if public_base_url:
            url = f"{public_base_url.rstrip('/')}/audit/packs/{pack.work_item_id}"
        elif pack.repair.get("pack_url"):
            url = str(pack.repair["pack_url"])
        entries.append(pack.to_index_entry(path=rel, url=url))

    # World model first (ecosystem, then repos), then specialists by score.
    def _sort_key(e: dict[str, Any]) -> tuple:
        layer = e.get("layer") or "specialist"
        layer_rank = 0 if layer == "world_model" else 1
        kind = e.get("kind") or ""
        # ecosystem overview before per-repo world packs
        kind_rank = 0 if e.get("id") == "pack-world-ecosystem" else 1
        return (layer_rank, kind_rank, -float(e.get("score") or 0), e.get("repo", ""), e.get("id", ""))

    entries.sort(key=_sort_key)
    by_kind: dict[str, int] = {}
    for e in entries:
        k = str(e.get("kind") or "unknown")
        by_kind[k] = by_kind.get(k, 0) + 1
    world_ids = [e["id"] for e in entries if e.get("layer") == "world_model"]
    specialist_ids = [e["id"] for e in entries if e.get("layer") != "world_model"]
    by_status: dict[str, int] = {}
    for e in entries:
        st = str(e.get("status") or DEFAULT_PACK_STATUS)
        by_status[st] = by_status.get(st, 0) + 1
    payload = {
        "updated_at": utc_now_iso(),
        "count": len(entries),
        "loop": "scan → compressed world model → specialist coding packs",
        "layers": {
            "world_model": world_ids,
            "specialist": specialist_ids,
        },
        "kind_counts": by_kind,
        "status_counts": by_status,
        "discovery": dict(PACK_DISCOVERY),
        "packs": entries,
    }
    index_path(output_dir).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_pack(output_dir: str | Path, pack_id: str) -> dict[str, Any] | None:
    path = pack_file_path(output_dir, pack_id)
    if not path.exists():
        # Fall back to scanning INDEX / yaml files for id match
        for yaml_path in packs_dir(output_dir).glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(data, dict) and data.get("work_item_id") == pack_id:
                return data
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def list_packs(
    output_dir: str | Path,
    *,
    repo: str = "",
    severity: str = "",
    kind: str = "",
    layer: str = "",
    q: str = "",
    run_id: str = "",
    status: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter INDEX entries. ``status`` may be comma-separated (e.g. ``open,claimed``)."""
    idx_file = index_path(output_dir)
    if idx_file.exists():
        try:
            payload = json.loads(idx_file.read_text(encoding="utf-8"))
            entries = list(payload.get("packs") or [])
        except (OSError, json.JSONDecodeError):
            entries = rebuild_index(output_dir).get("packs", [])
    else:
        entries = rebuild_index(output_dir).get("packs", [])

    repo_l = repo.lower().strip()
    sev_l = severity.upper().strip()
    kind_l = kind.lower().strip()
    layer_l = layer.lower().strip()
    q_l = q.lower().strip()
    run_l = run_id.strip()
    status_set = {s.strip().lower() for s in status.split(",") if s.strip()}

    out: list[dict[str, Any]] = []
    for entry in entries:
        if status_set and str(entry.get("status") or DEFAULT_PACK_STATUS).lower() not in status_set:
            continue
        if repo_l and repo_l not in str(entry.get("repo", "")).lower():
            # World-model ecosystem pack applies to all repos
            if not (
                entry.get("kind") == "world_model"
                and entry.get("id") == "pack-world-ecosystem"
            ):
                continue
        if sev_l and str(entry.get("severity", "")).upper() != sev_l:
            continue
        if kind_l and kind_l not in str(entry.get("kind", "")).lower():
            continue
        if layer_l and layer_l != str(entry.get("layer", "")).lower():
            continue
        if run_l and run_l != str(entry.get("run_id", "")):
            continue
        if q_l:
            hay = " ".join(
                str(entry.get(k, ""))
                for k in ("id", "title", "repo", "file_path", "kind", "layer")
            ).lower()
            if q_l not in hay:
                continue
        out.append(entry)
        if len(out) >= max(1, limit):
            break
    return out


def update_pack_status(
    output_dir: str | Path,
    pack_id: str,
    status: str,
    note: str = "",
    *,
    public_base_url: str = "",
    repair_updates: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Persist a lifecycle status on the pack YAML and refresh INDEX.json.

    Returns the updated pack dict, or ``None`` when the pack does not exist.
    Raises ``ValueError`` for an unknown status.
    """
    if status not in PACK_STATUSES:
        raise ValueError(f"invalid pack status {status!r}; expected one of {PACK_STATUSES}")
    data = load_pack(output_dir, pack_id)
    if data is None:
        return None
    pack = AgentCodingPack.from_dict(data, fallback_id=pack_id)
    pack.apply_status(status, (note or "")[:2000])
    if repair_updates:
        pack.repair.update(repair_updates)
    write_pack(pack, output_dir, preserve_status=False)
    rebuild_index(output_dir, public_base_url=public_base_url)
    return pack.to_dict()


def snapshot_pack_lifecycle(output_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Capture non-default lifecycle state so a full re-emit can restore it."""
    snap: dict[str, dict[str, Any]] = {}
    for yaml_path in packs_dir(output_dir).glob("*.yaml"):
        data = _read_pack_yaml(yaml_path)
        if not data or not data.get("work_item_id"):
            continue
        pack = AgentCodingPack.from_dict(data)
        if pack.status == DEFAULT_PACK_STATUS and not pack.status_history:
            continue
        snap[pack.work_item_id] = {
            "status": pack.status,
            "status_note": pack.status_note,
            "status_updated_at": pack.status_updated_at,
            "status_history": pack.status_history,
            "draft_pr_url": pack.repair.get("draft_pr_url"),
        }
    return snap


def restore_pack_lifecycle(
    output_dir: str | Path,
    snapshot: dict[str, dict[str, Any]],
    *,
    public_base_url: str = "",
) -> int:
    """Re-apply a :func:`snapshot_pack_lifecycle` onto packs that were re-emitted."""
    restored = 0
    for pack_id, state in snapshot.items():
        data = load_pack(output_dir, pack_id)
        if data is None:
            continue
        pack = AgentCodingPack.from_dict(data, fallback_id=pack_id)
        pack.status = state.get("status") or DEFAULT_PACK_STATUS
        pack.status_note = str(state.get("status_note") or "")
        pack.status_updated_at = str(state.get("status_updated_at") or "")
        pack.status_history = list(state.get("status_history") or [])
        if state.get("draft_pr_url") and not pack.repair.get("draft_pr_url"):
            pack.repair["draft_pr_url"] = state["draft_pr_url"]
        write_pack(pack, output_dir, preserve_status=False)
        restored += 1
    if restored:
        rebuild_index(output_dir, public_base_url=public_base_url)
    return restored


def pack_rag_ingest_enabled(explicit: bool | None = None) -> bool:
    """Resolve whether packs should be pushed to RAG.

    Default on. ``AI_AUDIT_PACK_RAG_INGEST=0`` (or false/no/off) is a kill switch that
    wins over everything; otherwise an explicit ``True``/``False`` (``--no-rag``) is honored.
    """
    env = os.getenv("AI_AUDIT_PACK_RAG_INGEST", "").strip().lower()
    if env in {"0", "false", "no", "off"}:
        return False
    return True if explicit is None else bool(explicit)


def _truncate_text(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    keep = max(0, cap - len(_TRUNCATION_MARK))
    return text[:keep].rstrip() + _TRUNCATION_MARK, True


def _world_brief_md(data: dict[str, Any], cap: int) -> tuple[str, bool]:
    narrative = data.get("narrative") or {}
    title = narrative.get("title") or data.get("work_item_id", "")
    text = "\n".join(
        (
            f"# {title} (`{data.get('work_item_id', '')}`)",
            str(narrative.get("recommendation") or ""),
            "",
            str(narrative.get("description") or ""),
        )
    )
    return _truncate_text(text, cap)


def _specialist_rank(entry: dict[str, Any], *, repo: str, q: str) -> float | None:
    """Higher is better; ``None`` means the pack is unrelated to *repo*."""
    primary = str(entry.get("repo") or "") == repo
    impacted = repo in (entry.get("impacted_repos") or [])
    if not (primary or impacted):
        return None
    sev = _SEVERITY_SCORE.get(str(entry.get("severity") or "MEDIUM").upper(), 0.3)
    score = 0.5 * sev + 0.5 * float(entry.get("score") or 0.0)
    score += 1.0 if primary else 0.3
    if q:
        hay = " ".join(
            str(entry.get(k, "")) for k in ("id", "title", "file_path", "kind")
        ).lower()
        if q.lower() in hay:
            score += 0.25
    # Prefer work nobody has picked up yet.
    status = str(entry.get("status") or DEFAULT_PACK_STATUS)
    if status == "claimed":
        score -= 0.15
    elif status == "in_progress":
        score -= 0.3
    return score


def build_pack_bundle(
    output_dir: str | Path,
    *,
    repo: str,
    token_budget: int = DEFAULT_BUNDLE_TOKEN_BUDGET,
    q: str = "",
    limit_specialists: int = 5,
    status: str = ",".join(ACTIVE_PACK_STATUSES),
    public_base_url: str = "",
) -> dict[str, Any]:
    """One-call mission bundle for a consuming agent working in *repo*.

    World model (ecosystem + architecture memory + ``pack-world-<repo>``) plus the
    top-ranked specialist packs, with ``briefs_markdown`` capped at roughly
    ``token_budget`` tokens (``CHARS_PER_TOKEN`` chars each). Lowest-ranked specialists
    are dropped first; the ecosystem world pack is never dropped (only truncated).
    """
    repo = repo.strip()
    char_budget = max(400, int(token_budget) * CHARS_PER_TOKEN)
    index_entries = list_packs(output_dir, status=status, layer="specialist", limit=10_000)
    ranked = sorted(
        (
            (rank, entry)
            for entry in index_entries
            if (rank := _specialist_rank(entry, repo=repo, q=q)) is not None
        ),
        key=lambda t: (-t[0], str(t[1].get("id", ""))),
    )
    candidates = len(ranked)
    ranked = ranked[: max(0, limit_specialists)]

    sections: list[str] = []
    used = 0
    truncated = False
    world_model: list[dict[str, Any]] = []
    # (pack id, share of the char budget it may use)
    for pack_id, share in (
        (WORLD_ECOSYSTEM_ID, 0.30),
        (f"pack-world-{repo}", 0.25),
        (WORLD_MEMORY_ID, 0.15),
    ):
        data = load_pack(output_dir, pack_id)
        if data is None:
            continue
        world_model.append(data)
        cap = min(int(char_budget * share), char_budget - used)
        if pack_id != WORLD_ECOSYSTEM_ID and cap < 200:
            truncated = True
            continue
        md, cut = _world_brief_md(data, max(cap, 200))
        sections.append(md)
        used += len(md) + 2
        truncated = truncated or cut
    # Stable world-model order for consumers: ecosystem, architecture memory, repo.
    order = {WORLD_ECOSYSTEM_ID: 0, WORLD_MEMORY_ID: 1}
    world_model.sort(key=lambda d: order.get(str(d.get("work_item_id")), 2))

    specialists: list[dict[str, Any]] = []
    included: list[str] = []
    dropped: list[str] = []
    budget_exhausted = False
    for position, (rank, entry) in enumerate(ranked, start=1):
        data = load_pack(output_dir, str(entry.get("id") or ""))
        if data is None:
            continue
        pack = AgentCodingPack.from_dict(data, fallback_id=str(entry.get("id") or ""))
        in_brief = False
        if not budget_exhausted:
            remaining = char_budget - used
            full = pack.to_brief_md()
            compact = pack.to_compact_md()
            if len(full) <= remaining:
                md = full
            elif len(compact) <= remaining:
                md, truncated = compact, True
            else:
                md = ""
                budget_exhausted = True  # drop this and every lower-ranked pack
            if md:
                sections.append(md)
                used += len(md) + 2
                in_brief = True
        if not in_brief:
            truncated = True
            dropped.append(pack.work_item_id)
        else:
            included.append(pack.work_item_id)
        specialists.append(
            {**pack.to_dict(), "bundle_rank": position, "rank_score": round(rank, 3), "in_brief": in_brief}
        )

    base = public_base_url.rstrip("/")
    discovery = {k: f"{base}{v}" if base else v for k, v in PACK_DISCOVERY.items()}
    discovery["bundle"] = discovery["bundle"].replace("{repo}", repo)
    briefs_markdown = "\n\n".join(sections)
    return {
        "repo": repo,
        "world_model": world_model,
        "specialists": specialists,
        "briefs_markdown": briefs_markdown,
        "truncated": truncated,
        "token_budget": int(token_budget),
        "char_budget": char_budget,
        "approx_tokens": len(briefs_markdown) // CHARS_PER_TOKEN,
        "candidates": candidates,
        "included_ids": included,
        "dropped_ids": dropped,
        "discovery": discovery,
        "hints": [
            "Read briefs_markdown first; fetch GET /audit/packs/{id} for full evidence.",
            "Claim work: POST /audit/packs/{id}/status {\"status\": \"claimed\"} (mutating auth).",
            "After a fix: POST /audit/validate_patch with pack_id to auto-resolve the pack.",
            "Mark non-actionable packs: POST /audit/packs/{id}/status {\"status\": \"wontfix\", \"note\": \"…\"}.",
        ],
    }


def emit_packs_from_worker_results(
    *,
    output_dir: str | Path,
    run_id: str,
    tasks: list[Any],
    results_by_id: dict[str, Any],
    draft_pr_by_repo: dict[str, str] | None = None,
    draft_pr_by_pack: dict[str, str] | None = None,
    public_base_url: str = "",
    ingest_rag: bool = False,
) -> list[AgentCodingPack]:
    """Write one pack per confirmed worker finding and refresh INDEX.json.

    ``draft_pr_by_pack`` maps pack id → draft PR URL; those packs are marked
    ``in_progress`` with ``repair.draft_pr_url`` set.
    """
    packs: list[AgentCodingPack] = []
    draft_pr_by_repo = draft_pr_by_repo or {}
    draft_pr_by_pack = draft_pr_by_pack or {}
    for task in tasks:
        result = results_by_id.get(task.id)
        if result is None:
            continue
        findings = [f for f in (result.findings or []) if isinstance(f, dict)]
        if not findings:
            continue
        handoff = str(Path(output_dir) / "conductor" / run_id / f"{task.id}.json")
        for finding in findings:
            pack = build_pack_from_worker_finding(
                finding,
                repo=task.repo,
                repo_path=task.repo_path,
                mission=task.mission,
                run_id=run_id,
                task_id=task.id,
                worker_summary=result.summary,
                draft_pr_url=draft_pr_by_repo.get(task.repo),
                handoff_path=handoff,
                public_base_url=public_base_url,
            )
            pr_url = draft_pr_by_pack.get(pack.work_item_id)
            if pr_url:
                pack.repair["draft_pr_url"] = pr_url
                pack.apply_status("in_progress", f"Draft PR opened: {pr_url}")
            write_pack(pack, output_dir)
            packs.append(pack)

    rebuild_index(output_dir, public_base_url=public_base_url)
    if ingest_rag and packs:
        ingest_packs_to_rag(packs)
    return packs


def emit_packs_from_findings(
    findings: list[Finding],
    *,
    output_dir: str | Path,
    repo_path: str | Path = "",
    kind: str = "diff_audit",
    run_id: str = "",
    directly_changed: list[str] | None = None,
    transitively_affected: list[str] | None = None,
    public_base_url: str = "",
    ingest_rag: bool = False,
    repo_paths: dict[str, str | Path] | None = None,
) -> list[AgentCodingPack]:
    packs: list[AgentCodingPack] = []
    paths = repo_paths or {}
    for finding in findings:
        root = paths.get(finding.repo, repo_path)
        pack = build_pack_from_finding(
            finding,
            repo_path=root,
            kind=kind,
            run_id=run_id,
            directly_changed=directly_changed,
            transitively_affected=transitively_affected,
            public_base_url=public_base_url,
        )
        write_pack(pack, output_dir)
        packs.append(pack)
    rebuild_index(output_dir, public_base_url=public_base_url)
    if ingest_rag and packs:
        ingest_packs_to_rag(packs)
    return packs


def finding_from_violation(violation: RuleViolation) -> Finding:
    """Map an architecture RuleViolation onto the Finding contract for packing."""
    repo = ""
    file_path = ""
    line = 0
    # Forms: "repo: path/file.py:123", "repo/path.py", "a → b", "vidita contains …"
    vp = violation.violating_path or ""
    m_repo_file = re.match(
        r"^(?P<repo>[\w.-]+):\s*(?P<path>[\w./-]+\.[a-zA-Z0-9]+):(?P<line>\d+)\s*$",
        vp,
    )
    if m_repo_file:
        repo = m_repo_file.group("repo")
        file_path = m_repo_file.group("path")
        line = int(m_repo_file.group("line"))
    elif "→" in vp or "->" in vp:
        repo = (violation.entities[0] if violation.entities else vp.split("→")[0].split("->")[0]).strip()
        m = re.search(r"([\w./-]+\.[a-zA-Z0-9]+):(\d+)", violation.evidence or "")
        if m:
            file_path = m.group(1)
            line = int(m.group(2))
    elif violation.entities:
        repo = violation.entities[0]
        file_path = vp if ("/" in vp or re.search(r"\.\w+$", vp)) and ":" not in vp.split("/")[0] else ""
        if not file_path:
            m = re.search(r"([\w./-]+\.[a-zA-Z0-9]+):(\d+)", vp + " " + (violation.evidence or ""))
            if m:
                file_path = m.group(1)
                line = int(m.group(2))
    else:
        parts = vp.split("/", 1)
        if len(parts) == 2 and parts[0]:
            repo, file_path = parts[0], parts[1]
        else:
            repo = vp or "unknown"

    title = vp if len(vp) <= 120 else f"{violation.rule_name}: {vp[:100]}"
    return Finding(
        id="",
        type=violation.rule_name or "architecture",
        severity=_normalize_severity(violation.severity),
        repo=repo,
        file_path=file_path,
        line=line,
        title=title,
        description=violation.evidence,
        evidence=violation.evidence,
        recommendation=(
            f"Resolve architecture rule `{violation.rule_name}` involving "
            f"{', '.join(violation.entities) or repo}."
        ),
        source="architecture",
        detected_at=violation.detected_at or utc_now_iso(),
    )


def emit_packs_from_violations(
    violations: list[RuleViolation],
    *,
    output_dir: str | Path,
    run_id: str = "",
    public_base_url: str = "",
    ingest_rag: bool = False,
    repo_paths: dict[str, str | Path] | None = None,
    limit: int = 50,
) -> list[AgentCodingPack]:
    findings = [finding_from_violation(v) for v in violations[: max(1, limit)]]
    return emit_packs_from_findings(
        findings,
        output_dir=output_dir,
        kind="architecture",
        run_id=run_id,
        public_base_url=public_base_url,
        ingest_rag=ingest_rag,
        repo_paths=repo_paths,
    )


def emit_world_model_packs(
    *,
    output_dir: str | Path,
    run_id: str = "",
    public_base_url: str = "",
    ingest_rag: bool = False,
    include_repos: bool = True,
) -> list[AgentCodingPack]:
    """Compressed world model: ecosystem overview + per-repo briefings + architecture memory."""
    from auditor.config import load_ecosystem_config
    from auditor.ecosystem_briefing import (
        _get_repo_roles,
        build_ecosystem_overview,
        build_repo_briefing,
    )
    from auditor.repo_resolver import resolve_repo_path
    from ecosystem.graph_builder import GraphBuilder

    cfg = load_ecosystem_config()
    base = public_base_url.rstrip("/") if public_base_url else ""
    packs: list[AgentCodingPack] = []
    now = utc_now_iso()

    overview = build_ecosystem_overview(max_chars=8000)
    eco = AgentCodingPack(
        work_item_id="pack-world-ecosystem",
        kind="world_model",
        priority={"severity": "INFO", "score": 1.0},
        repo={"name": "ecosystem", "path": "", "base_branch": "dev"},
        location={"file_path": "ecosystem/ecosystem.md", "line": 1, "symbol": "", "code_snippet": "", "evidence": ""},
        narrative={
            "title": "Argus ecosystem world model",
            "description": overview,
            "recommendation": (
                "Load this pack before specialist work. Respect cortex ownership, "
                "agent-composer for RAG/chat, voluptas for ComfyUI, scan branch dev."
            ),
            "acceptance_criteria": [
                "Do not reimplement argus-cortex contracts in demo UIs",
                "Route chat/RAG through agent-composer; generation workers call voluptas",
            ],
            "missing_evidence": "",
        },
        impact={"impacted_repos": [r.name for r in cfg.repos if r.enabled]},
        governance={"architecture_rules_hit": [], "related_findings": []},
        verification={"suggested_test_commands": [], "tool": "skipped", "max_iterations": 0},
        repair={
            "pack_url": f"{base}/audit/packs/pack-world-ecosystem" if base else "",
            "intent_markdown": "",
        },
        provenance={
            "source": "ecosystem_briefing",
            "run_id": run_id,
            "task_id": "",
            "detected_at": now,
            "handoff_path": "ecosystem/ecosystem.md",
        },
    )
    write_pack(eco, output_dir)
    packs.append(eco)

    mem_path = Path(cfg.architecture_memory_path)
    if mem_path.exists():
        mem_text = mem_path.read_text(encoding="utf-8", errors="replace")[:6000]
        mem = AgentCodingPack(
            work_item_id="pack-world-architecture-memory",
            kind="world_model",
            priority={"severity": "INFO", "score": 0.95},
            repo={"name": "ecosystem", "path": "", "base_branch": "dev"},
            location={
                "file_path": str(mem_path),
                "line": 1,
                "symbol": "",
                "code_snippet": "",
                "evidence": mem_text[:1500],
            },
            narrative={
                "title": "Architecture memory (durable decisions)",
                "description": mem_text,
                "recommendation": "Treat these decisions as constraints unless explicitly superseded.",
                "acceptance_criteria": ["Do not revive out-of-scope legacy stacks"],
                "missing_evidence": "",
            },
            verification={"suggested_test_commands": [], "tool": "skipped", "max_iterations": 0},
            repair={"pack_url": f"{base}/audit/packs/pack-world-architecture-memory" if base else ""},
            provenance={
                "source": "architecture_memory",
                "run_id": run_id,
                "detected_at": now,
                "handoff_path": str(mem_path),
            },
        )
        write_pack(mem, output_dir)
        packs.append(mem)

    if include_repos:
        graph = GraphBuilder(cfg.graph_db_path)
        try:
            roles = _get_repo_roles()
            for repo in cfg.repos:
                if not repo.enabled:
                    continue
                try:
                    root = resolve_repo_path(repo)
                except Exception:
                    continue
                if not root.exists():
                    continue
                briefing = build_repo_briefing(repo.name, root, graph=graph, max_chars=4000)
                role = roles.get(repo.name, {})
                pack_id = f"pack-world-{repo.name}"
                tool, cmds = suggest_test_commands(root)
                pack = AgentCodingPack(
                    work_item_id=pack_id,
                    kind="world_model",
                    priority={"severity": "INFO", "score": 0.9},
                    repo={"name": repo.name, "path": str(root), "base_branch": repo.branch or "dev"},
                    location={
                        "file_path": "ecosystem/ecosystem.md",
                        "line": 0,
                        "symbol": "",
                        "code_snippet": "",
                        "evidence": role.get("role", ""),
                    },
                    narrative={
                        "title": f"World model: {repo.name}",
                        "description": briefing,
                        "recommendation": (
                            f"Use as compressed context when working in {repo.name}. "
                            f"Role: {role.get('label', '')} — {role.get('role', '')}"
                        ),
                        "acceptance_criteria": [
                            f"Stay within {repo.name} ownership unless cross-repo pack says otherwise",
                        ],
                        "missing_evidence": "",
                    },
                    impact={"impacted_repos": [repo.name]},
                    verification={
                        "suggested_test_commands": cmds[:2],
                        "tool": tool,
                        "max_iterations": 0,
                    },
                    repair={"pack_url": f"{base}/audit/packs/{pack_id}" if base else ""},
                    provenance={
                        "source": "repo_briefing",
                        "run_id": run_id,
                        "detected_at": now,
                        "handoff_path": str(root),
                    },
                )
                write_pack(pack, output_dir)
                packs.append(pack)
        finally:
            graph.close()

    rebuild_index(output_dir, public_base_url=public_base_url)
    if ingest_rag and packs:
        ingest_packs_to_rag(packs)
    return packs


def emit_packs_from_extraction_candidates(
    candidates: list[dict[str, Any]] | None = None,
    *,
    output_dir: str | Path,
    candidates_path: str | Path | None = None,
    run_id: str = "",
    public_base_url: str = "",
    ingest_rag: bool = False,
    limit: int = 20,
) -> list[AgentCodingPack]:
    """Specialist packs for shared-logic extraction / feature consolidation work."""
    items: list[dict[str, Any]] = list(candidates or [])
    if not items:
        path = Path(candidates_path or Path(output_dir) / "extraction_candidates.yaml")
        if not path.exists():
            return []
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("cannot read extraction candidates: %s", exc)
            return []
        items = list(payload.get("candidates") or [])

    base = public_base_url.rstrip("/") if public_base_url else ""
    packs: list[AgentCodingPack] = []
    for cand in items[: max(1, limit)]:
        if not isinstance(cand, dict):
            continue
        cid = str(cand.get("id") or "")
        if not cid:
            continue
        pack_id = cid if cid.startswith("pack-") else f"pack-{cid}"
        repos = list(cand.get("repos_involved") or [])
        score = float(cand.get("extraction_score") or 0.0)
        title = str(cand.get("title") or f"Extraction {cid}")
        rationale = cand.get("rationale") or []
        plan = cand.get("migration_plan") or {}
        risks = cand.get("risks") or []
        source_units = cand.get("source_units") or []
        first = source_units[0] if source_units else {}
        desc_parts = [
            title,
            "",
            "Rationale:",
            *[f"- {r}" for r in rationale],
            "",
            f"Proposed library: {(cand.get('proposed_library') or {}).get('name', '')}",
            f"Public API: {(cand.get('proposed_library') or {}).get('public_api', [])}",
            "",
            "Migration plan:",
            yaml.safe_dump(plan, sort_keys=False)[:2000] if plan else "(none)",
            "",
            "Risks:",
            *[f"- {r}" for r in (risks if isinstance(risks, list) else [risks])],
        ]
        pack = AgentCodingPack(
            work_item_id=pack_id,
            kind="extraction",
            priority={
                "severity": "HIGH" if score >= 0.75 else "MEDIUM" if score >= 0.5 else "LOW",
                "score": min(1.0, score if score > 0 else 0.4),
            },
            repo={
                "name": repos[0] if repos else str(first.get("repo") or "ecosystem"),
                "path": "",
                "base_branch": "dev",
            },
            location={
                "file_path": str(first.get("path") or ""),
                "line": 0,
                "symbol": ",".join(first.get("symbols") or []),
                "code_snippet": "",
                "evidence": json.dumps(source_units[:5], ensure_ascii=False)[:2000],
            },
            narrative={
                "title": title,
                "description": "\n".join(str(p) for p in desc_parts),
                "recommendation": f"Execute phased extraction for {cid}; validate adapters before rollout.",
                "acceptance_criteria": [
                    "Create shared package / adapters without behavior change",
                    f"Migrate repos in order: {', '.join(repos)}",
                    "Add/confirm architecture rule requiring the shared package",
                ],
                "missing_evidence": "",
            },
            impact={
                "impacted_repos": repos,
                "call_path": [],
                "directly_changed": [str(u.get("path") or "") for u in source_units if u.get("path")],
            },
            governance={
                "architecture_rules_hit": [],
                "related_findings": [],
                "extraction_candidate_id": cid,
            },
            verification={
                "suggested_test_commands": ["pytest -q --tb=short"],
                "tool": "pytest",
                "max_iterations": 3,
                "validate_endpoint": "POST /audit/validate_patch",
            },
            repair={
                "pack_url": f"{base}/audit/packs/{pack_id}" if base else "",
                "slug": f"repair/{cid}",
            },
            provenance={
                "source": "extraction_candidates",
                "run_id": run_id,
                "detected_at": utc_now_iso(),
                "handoff_path": str(candidates_path or Path(output_dir) / "extraction_candidates.yaml"),
            },
        )
        write_pack(pack, output_dir)
        packs.append(pack)

    if packs:
        rebuild_index(output_dir, public_base_url=public_base_url)
        if ingest_rag:
            ingest_packs_to_rag(packs)
    return packs


def ingest_packs_to_rag(packs: list[AgentCodingPack]) -> dict[str, Any]:
    """Upsert slim pack summaries into audit_docs for agent retrieval."""
    import requests

    url = os.getenv("AI_AUDIT_RAG_INGEST_URL") or os.getenv("RAG_INGEST_URL") or "http://127.0.0.1:9050/ingest"
    secret = os.getenv("INGEST_SHARED_SECRET", "")
    collection = os.getenv("AI_AUDIT_RAG_COLLECTION", "audit_docs")
    documents = []
    for pack in packs:
        documents.append(
            {
                "id": f"agent-pack::{pack.work_item_id}",
                "text": pack.to_rag_text(),
                "metadata": {
                    "source": "ai-code-auditor",
                    "kind": "agent_coding_pack",
                    "pack_kind": pack.kind,
                    "layer": pack.layer(),
                    "pack_id": pack.work_item_id,
                    "repo": (pack.repo or {}).get("name", ""),
                    "severity": (pack.priority or {}).get("severity", ""),
                    "status": pack.status or DEFAULT_PACK_STATUS,
                },
            }
        )
    if not documents:
        return {"ingested": False, "skipped": True, "count": 0}

    result: dict[str, Any] = {"ingested": True, "skipped": False, "count": 0}
    for start in range(0, len(documents), _RAG_BATCH_SIZE):
        batch = documents[start : start + _RAG_BATCH_SIZE]
        payload = json.dumps({"collection": collection, "documents": batch}).encode()
        ts = int(time.time())
        headers = {"Content-Type": "application/json", "X-Timestamp": str(ts)}
        if secret:
            headers["X-Signature"] = hmac.new(
                secret.encode(), f"{ts}:".encode() + payload, hashlib.sha256
            ).hexdigest()
        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=30)
        except requests.RequestException as exc:
            logger.warning("pack RAG ingest failed: %s", exc)
            return {**result, "ingested": False, "error": str(exc)}
        if not 200 <= resp.status_code < 300:
            logger.warning("pack RAG ingest HTTP %s: %s", resp.status_code, (resp.text or "")[:300])
            return {**result, "ingested": False, "status_code": resp.status_code}
        result["count"] += len(batch)
        result["status_code"] = resp.status_code
    return result
