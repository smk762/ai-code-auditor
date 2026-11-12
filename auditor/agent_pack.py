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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pack_version"] = self.pack_version
        return d

    def to_index_entry(self, *, path: str, url: str = "") -> dict[str, Any]:
        return {
            "id": self.work_item_id,
            "kind": self.kind,
            "severity": (self.priority or {}).get("severity", "MEDIUM"),
            "score": (self.priority or {}).get("score", 0.0),
            "repo": (self.repo or {}).get("name", ""),
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

    def to_rag_text(self) -> str:
        """Slim text for audit_docs retrieval (not a full dump)."""
        entry = self.to_index_entry(path="")
        parts = [
            f"Agent coding pack {self.work_item_id}",
            f"kind={self.kind} severity={entry['severity']} repo={entry['repo']}",
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


def write_pack(pack: AgentCodingPack, output_dir: str | Path) -> Path:
    root = packs_dir(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = pack_file_path(output_dir, pack.work_item_id)
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
        pack = AgentCodingPack(
            work_item_id=str(data["work_item_id"]),
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
        )
        rel = str(yaml_path.relative_to(Path(output_dir)))
        url = ""
        if public_base_url:
            url = f"{public_base_url.rstrip('/')}/audit/packs/{pack.work_item_id}"
        elif pack.repair.get("pack_url"):
            url = str(pack.repair["pack_url"])
        entries.append(pack.to_index_entry(path=rel, url=url))

    entries.sort(key=lambda e: (-float(e.get("score") or 0), e.get("repo", ""), e.get("id", "")))
    payload = {
        "updated_at": utc_now_iso(),
        "count": len(entries),
        "discovery": {
            "list": "/audit/packs",
            "get": "/audit/packs/{id}",
            "brief": "/audit/packs/{id}/brief",
            "index_file": "agent_packs/INDEX.json",
        },
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
    q: str = "",
    run_id: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
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
    q_l = q.lower().strip()
    run_l = run_id.strip()

    out: list[dict[str, Any]] = []
    for entry in entries:
        if repo_l and repo_l not in str(entry.get("repo", "")).lower():
            continue
        if sev_l and str(entry.get("severity", "")).upper() != sev_l:
            continue
        if kind_l and kind_l not in str(entry.get("kind", "")).lower():
            continue
        if run_l and run_l != str(entry.get("run_id", "")):
            continue
        if q_l:
            hay = " ".join(
                str(entry.get(k, "")) for k in ("id", "title", "repo", "file_path", "kind")
            ).lower()
            if q_l not in hay:
                continue
        out.append(entry)
        if len(out) >= max(1, limit):
            break
    return out


def emit_packs_from_worker_results(
    *,
    output_dir: str | Path,
    run_id: str,
    tasks: list[Any],
    results_by_id: dict[str, Any],
    draft_pr_by_repo: dict[str, str] | None = None,
    public_base_url: str = "",
    ingest_rag: bool = False,
) -> list[AgentCodingPack]:
    """Write one pack per confirmed worker finding and refresh INDEX.json."""
    packs: list[AgentCodingPack] = []
    draft_pr_by_repo = draft_pr_by_repo or {}
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


def ingest_packs_to_rag(packs: list[AgentCodingPack]) -> dict[str, Any]:
    """Upsert slim pack summaries into audit_docs for agent retrieval."""
    import requests

    url = os.getenv("AI_AUDIT_RAG_INGEST_URL") or os.getenv("RAG_INGEST_URL") or "http://127.0.0.1:9050/ingest"
    secret = os.getenv("INGEST_SHARED_SECRET", "")
    collection = os.getenv("AI_AUDIT_RAG_COLLECTION", "audit_docs")
    documents = []
    for pack in packs[:40]:
        documents.append(
            {
                "id": f"agent-pack::{pack.work_item_id}",
                "text": pack.to_rag_text(),
                "metadata": {
                    "source": "ai-code-auditor",
                    "kind": "agent_coding_pack",
                    "pack_id": pack.work_item_id,
                    "repo": (pack.repo or {}).get("name", ""),
                    "severity": (pack.priority or {}).get("severity", ""),
                },
            }
        )
    if not documents:
        return {"ingested": False, "skipped": True, "count": 0}

    body = {"collection": collection, "documents": documents}
    payload = json.dumps(body).encode()
    ts = int(time.time())
    headers = {"Content-Type": "application/json", "X-Timestamp": str(ts)}
    if secret:
        headers["X-Signature"] = hmac.new(
            secret.encode(), f"{ts}:".encode() + payload, hashlib.sha256
        ).hexdigest()
    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=30)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("pack RAG ingest HTTP %s: %s", resp.status_code, (resp.text or "")[:300])
        return {"ingested": ok, "skipped": False, "status_code": resp.status_code, "count": len(documents)}
    except requests.RequestException as exc:
        logger.warning("pack RAG ingest failed: %s", exc)
        return {"ingested": False, "skipped": False, "error": str(exc), "count": len(documents)}
