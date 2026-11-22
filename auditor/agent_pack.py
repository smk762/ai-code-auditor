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
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # libyaml's C loader parses packs several times faster than the pure-Python one
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # pragma: no cover — PyYAML built without libyaml
    from yaml import SafeLoader as _YamlLoader

from auditor.contracts import Finding, RuleViolation, utc_now_iso
from auditor.fsutil import atomic_write_text
from auditor.finding_match import reworded_score
from auditor.validator import suggest_test_commands

logger = logging.getLogger(__name__)

PACK_VERSION = 1
_SNIPPET_RADIUS = 12
_SEVERITY_SCORE = {"CRITICAL": 1.0, "HIGH": 0.85, "MEDIUM": 0.55, "LOW": 0.3, "INFO": 0.1}

PACK_STATUSES = ("open", "claimed", "in_progress", "unverified", "resolved", "wontfix", "obsolete")
DEFAULT_PACK_STATUS = "open"
# Statuses a consuming agent should still pick up (resolved/wontfix are closed).
# unverified: validate_patch passed without the evidence the pack needs (#58); still open work.
ACTIVE_PACK_STATUSES = ("open", "claimed", "in_progress", "unverified")
_STATUS_HISTORY_MAX = 20
# Dedupe: a re-found issue (same repo and file, reworded title, earlier emit) reuses the existing
# pack. Resolved packs are not merge targets: an issue that comes back after a fix gets a fresh
# open pack instead of hiding under "resolved". Kinds whose titles don't describe the issue
# (architecture titles are "repo: path:line") dedupe on exact id only.
# obsolete (closed by pack GC, #67) is a target so a re-found issue reopens its own pack instead of a twin.
_MERGE_INTO_STATUSES = ("in_progress", "claimed", "unverified", "wontfix", "open", "obsolete")  # keeper preference order
_NO_TITLE_MATCH_KINDS = frozenset({"architecture", "extraction", "world_model", "invariants", "class_guard"})

# Bundle sizing: token_budget is approximated as chars / CHARS_PER_TOKEN.
CHARS_PER_TOKEN = 4
DEFAULT_BUNDLE_TOKEN_BUDGET = 2500
WORLD_ECOSYSTEM_ID = "pack-world-ecosystem"
WORLD_MEMORY_ID = "pack-world-architecture-memory"
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
            "line": int((self.location or {}).get("line") or 0),
            "duplicate_of": (self.governance or {}).get("duplicate_of") or "",
            "recurrence_of": (self.governance or {}).get("recurrence_of") or "",
            "finding_class": ((self.governance or {}).get("finding_class") or {}).get("id") or "",
            "class_parent": (self.governance or {}).get("class_parent") or "",
            "class_exception": bool((self.governance or {}).get("class_exception")),
            "run_id": (self.provenance or {}).get("run_id", ""),
            "path": path,
            "url": url,
            "detected_at": (self.provenance or {}).get("detected_at", ""),
            "status_updated_at": self.status_updated_at or "",
            "draft_pr_url": (self.repair or {}).get("draft_pr_url") or "",
        }

    def to_brief_md(self) -> str:
        sev = (self.priority or {}).get("severity", "MEDIUM")
        title = _one_line((self.narrative or {}).get("title", self.work_item_id))
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
            *([f"- **class:** `{fc_}` (guard task: `{(self.governance or {}).get('class_parent')}`)"]
              if (fc_ := ((self.governance or {}).get("finding_class") or {}).get("id"))
              and (self.governance or {}).get("class_parent") else []),
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
            lines.extend(_fenced(str(evidence)[:2000]))
        snippet = loc.get("code_snippet") or ""
        if snippet:
            lines.extend(["", "## Code snippet", *_fenced(str(snippet)[:3000])])
        impact = self.impact or {}
        call_path = impact.get("call_path") or []
        if call_path:
            lines.extend(["", "## Call path", *[f"- {p}" for p in call_path]])
        verify = self.verification or {}
        cmds = verify.get("suggested_test_commands") or []
        if cmds:
            lines.extend(["", "## Verify", *[f"- `{c}`" for c in cmds]])
        lines.extend(["", "## Done means", f"- {self.done_means()}"])
        last = verify.get("evidence") or {}
        if self.status == "unverified" and last.get("reason"):
            lines.append(f"- Last validate_patch passed but didn't prove the fix: {last['reason']}")
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

    def done_means(self, *, short: bool = False) -> str:
        """The evidence validate_patch needs before it resolves this pack (#58). *short* is the
        one-liner for compact briefs, where every character comes out of the bundle budget."""
        from auditor.evidence import needs_repro

        if self.kind == "world_model":
            return "Nothing to close: this is context, not work."
        repro = needs_repro({"kind": self.kind, "narrative": self.narrative})
        if short:
            return "a test failing on dev, passing with the fix" if repro else "tests pass (lint alone: unverified)"
        if repro:
            return ("Add or change a test that fails on `dev` and passes with your fix; validate_patch runs it on "
                    "both trees. Without one the pack becomes `unverified`. If no test can exercise the behaviour, "
                    "pass `evidence_waiver` with the reason.")
        return ("The project's tests (or npm checks) must run and pass; lint alone leaves the pack `unverified`. "
                "Docs- or config-only changes are exempt.")

    def to_compact_md(self) -> str:
        """Short brief (no snippet/evidence) used by bundles under a token budget."""
        title = _one_line((self.narrative or {}).get("title", self.work_item_id))
        loc = self.location or {}
        lines = [
            f"## {title}",
            f"`{self.work_item_id}` · {self.kind} · "
            f"{(self.priority or {}).get('severity', 'MEDIUM')} · status={self.status or DEFAULT_PACK_STATUS}",
            f"repo: {(self.repo or {}).get('name', '')} · "
            f"`{loc.get('file_path', '')}`:{loc.get('line', 0)}",
            "",
            (self.narrative or {}).get("recommendation") or (self.narrative or {}).get("description") or title,
            "",
            f"Done: {self.done_means(short=True)}",
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


def _fenced(text: str) -> list[str]:
    """*text* in a code fence it cannot close: one backtick longer than its longest backtick run.

    Evidence and snippets come from audited code and LLM output; a ``` line inside a fixed fence
    would end it and turn the rest into top-level brief text for the coding agent."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence, text, fence]


def _one_line(text: Any) -> str:
    """A title on one line, so it can't open new markdown sections under its heading."""
    return " ".join(str(text or "").split())


def packs_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / "agent_packs"


def index_path(output_dir: str | Path) -> Path:
    return packs_dir(output_dir) / "INDEX.json"


def pack_file_path(output_dir: str | Path, pack_id: str) -> Path:
    """File for ``pack_id``. Safe ids map to themselves, so existing pack files keep their names.

    An id that sanitizing would change (disallowed characters, edge dashes, over 120 chars, empty)
    gets a hash of the original id appended after ``~``, so two distinct ids can never share one
    file: ``~`` is never in a safe id, so a hashed name can't be spelled as a literal id.
    """
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", pack_id).strip("-")[:120]
    if safe != pack_id or not safe:
        digest = hashlib.sha1(pack_id.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        safe = f"{safe[:103]}~{digest}" if safe else f"pack~{digest}"
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
    # file_path comes from model output: only ever read inside the repo (no "..", no outside
    # absolute path, no symlink out), or the pack could quote the auditor host's secrets.
    try:
        root = Path(repo_path).resolve()
        path = (root / file_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    if not path.is_relative_to(root):
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    try:
        idx = max(1, int(line or 1)) - 1
    except (TypeError, ValueError):  # e.g. "12-14" or "N/A" from finding JSON
        idx = 0
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
    # No repo_path means no checkout to read: never fall back to the auditor's own cwd.
    root = Path(repo_path) if repo_path else None
    sev = _normalize_severity(finding.severity)
    pack_id = stable_pack_id(
        repo=finding.repo,
        file_path=finding.file_path,
        title=finding.title,
        source=finding.source,
        run_id=run_id,
        finding_id=finding.id,
    )
    have_root = root is not None and root.exists()
    tool, cmds = suggest_test_commands(root, finding.file_path) if have_root else ("skipped", [])
    snippet = read_code_snippet(root, finding.file_path, finding.line) if have_root else ""
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
            "finding_type": finding.type or "",
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
            "finding_id": finding.id,
            "run_id": run_id,
            "task_id": task_id,
            "detected_at": finding.detected_at or utc_now_iso(),
            "handoff_path": handoff_path,
        },
    )


def _worker_call_path(raw: Any) -> list[str]:
    """Worker ``call_path`` as a list; a single string is one hop chain, not a list of characters."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split("->") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw]
    return [str(raw)]


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
    line_m = re.match(r"\D*(\d+)", str(finding.get("line") or ""))  # "42", "42-50", "L42", "12.0"
    line = int(line_m.group(1)) if line_m else 0
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
        call_path=_worker_call_path(finding.get("call_path")),
        draft_pr_url=draft_pr_url,
        handoff_path=handoff_path,
        public_base_url=public_base_url,
        missing_evidence=str(finding.get("missing_evidence") or ""),
    )
    pack.location["symbol"] = str(finding.get("symbol") or "")
    pack.repair["intent_markdown"] = pack.to_brief_md()
    return pack


def _load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_YamlLoader)  # noqa: S506 — a safe loader


def _read_pack_yaml(path: Path) -> dict[str, Any] | None:
    try:
        data = _load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _carry_over_lifecycle(pack: AgentCodingPack, existing: dict[str, Any]) -> None:
    """Re-emitting a pack must not reset an agent-set status back to ``open``."""
    prev = AgentCodingPack.from_dict(existing, fallback_id=pack.work_item_id)
    if prev.governance.get("recurrence_of") and not pack.governance.get("recurrence_of"):
        pack.governance["recurrence_of"] = prev.governance["recurrence_of"]
    if prev.governance.get("finding_class") and not pack.governance.get("finding_class"):
        # The nightly classify step re-checks it against the text hash (#53)
        pack.governance["finding_class"] = prev.governance["finding_class"]
    for key in ("class_parent", "class_exception"):  # #54; the class step re-checks them nightly
        if prev.governance.get(key) and not pack.governance.get(key):
            pack.governance[key] = prev.governance[key]
    # A draft PR outlives every status path, an explicit re-emit status included (#7fb8684665)
    if not pack.repair.get("draft_pr_url") and prev.repair.get("draft_pr_url"):
        pack.repair["draft_pr_url"] = prev.repair["draft_pr_url"]
    if prev.status == DEFAULT_PACK_STATUS and not prev.status_history:
        return
    if pack.status != DEFAULT_PACK_STATUS:
        return  # caller set an explicit status (e.g. conductor in_progress)
    pack.status = prev.status
    pack.status_note = prev.status_note
    pack.status_updated_at = prev.status_updated_at
    pack.status_history = prev.status_history
    if prev.governance.get("duplicate_of"):
        pack.governance["duplicate_of"] = prev.governance["duplicate_of"]
    if prev.governance.get("gc"):
        pack.governance["gc"] = prev.governance["gc"]
    if pack.status == "obsolete":
        # GC closed it because no scan reported it; a scan just did, so it's open work again (#67)
        pack.apply_status("open", f"reported again after pack GC closed it ({prev.governance.get('gc', {}).get('rule', 'gc')})")


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
    # Atomic: a concurrent load_pack must never read half a YAML (it would 404 a real pack).
    atomic_write_text(path, yaml.safe_dump(pack.to_dict(), sort_keys=False, allow_unicode=True))
    # Sidecar brief for agents that prefer markdown
    atomic_write_text(path.with_suffix(".md"), pack.to_brief_md())
    return path


_INDEX_THREAD_LOCK = threading.RLock()
_index_lock_depth = threading.local()


@contextmanager
def pack_lock(output_dir: str | Path):
    """Serialise pack and INDEX.json read-modify-writes across API threads and other processes.

    Every writer that loads a pack, decides on its state and writes it back holds this for the whole
    load → check → write → index step, or a concurrent claim can be overwritten (GC, guards, emits).

    Re-entrant within a thread; the file lock is taken only at the outermost level."""
    with _INDEX_THREAD_LOCK:
        depth = getattr(_index_lock_depth, "n", 0)
        _index_lock_depth.n = depth + 1
        try:
            if depth:
                yield
                return
            root = packs_dir(output_dir)
            root.mkdir(parents=True, exist_ok=True)
            with open(root / ".INDEX.lock", "w") as fh:
                try:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_EX)
                except ImportError:  # pragma: no cover — non-POSIX: the thread lock alone
                    pass
                yield
        finally:
            _index_lock_depth.n = depth


def _index_entry(pack: AgentCodingPack, output_dir: str | Path, yaml_path: Path,
                 public_base_url: str = "") -> dict[str, Any]:
    rel = str(yaml_path.relative_to(Path(output_dir)))
    url = ""
    if public_base_url:
        url = f"{public_base_url.rstrip('/')}/audit/packs/{pack.work_item_id}"
    elif pack.repair.get("pack_url"):
        url = str(pack.repair["pack_url"])
    return pack.to_index_entry(path=rel, url=url)


def _write_index(output_dir: str | Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Sort *entries*, add the counts, and replace INDEX.json atomically (readers never see half)."""
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
    atomic_write_text(index_path(output_dir), json.dumps(payload, indent=2))
    return payload


def rebuild_index(output_dir: str | Path, *, public_base_url: str = "") -> dict[str, Any]:
    """Re-read every pack YAML and rewrite INDEX.json (emits and repairs; seconds on thousands of packs)."""
    root = packs_dir(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    with pack_lock(output_dir):
        entries: list[dict[str, Any]] = []
        for yaml_path in sorted(root.glob("*.yaml")):
            try:
                data = _load_yaml(yaml_path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError, yaml.YAMLError) as exc:  # ValueError: bad UTF-8
                logger.warning("skip bad pack %s: %s", yaml_path, exc)
                continue
            if not isinstance(data, dict) or not data.get("work_item_id"):
                continue
            try:  # one malformed pack must not leave INDEX.json stale for every other pack
                entries.append(_index_entry(AgentCodingPack.from_dict(data), output_dir, yaml_path, public_base_url))
            except Exception as exc:  # noqa: BLE001 — skip the pack, keep the rebuild
                logger.warning("skip malformed pack %s: %s", yaml_path, exc)
        return _write_index(output_dir, entries)


def upsert_index_entries(output_dir: str | Path, packs: list[AgentCodingPack], *,
                         public_base_url: str = "") -> None:
    """Replace these packs' INDEX.json entries in place (a status change, a GC pass, an emit),
    rebuilding only when the index is missing or unreadable. The caller holds ``pack_lock``.

    Cost is one read and one write of INDEX.json however many packs exist; a full rebuild re-parses
    every pack (seconds of GIL-bound work at thousands of packs, tests/test_hot_paths.py)."""
    if not packs:
        return
    try:
        doc = json.loads(index_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    entries = doc.get("packs") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        # Valid JSON of the wrong shape is as unreadable as a torn write: rebuild it
        rebuild_index(output_dir, public_base_url=public_base_url)
        return
    fresh = {p.work_item_id: _index_entry(p, output_dir, pack_file_path(output_dir, p.work_item_id), public_base_url)
             for p in packs}
    _write_index(output_dir, [e for e in entries if e.get("id") not in fresh] + list(fresh.values()))


def _upsert_index_entry(output_dir: str | Path, pack: AgentCodingPack, *, public_base_url: str = "") -> None:
    upsert_index_entries(output_dir, [pack], public_base_url=public_base_url)


def load_pack(output_dir: str | Path, pack_id: str) -> dict[str, Any] | None:
    path = pack_file_path(output_dir, pack_id)
    if not path.exists():
        # An id whose file name differs (sanitised or truncated) is found through INDEX.json.
        # Never by parsing every pack: that cost ~1.5 s per unknown id on the live set.
        try:
            entries = json.loads(index_path(output_dir).read_text(encoding="utf-8")).get("packs") or []
        except (OSError, ValueError):
            return None
        rel = next((e.get("path") for e in entries if e.get("id") == pack_id), None)
        if not rel:
            return None
        path = Path(output_dir) / rel
    try:
        data = _load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _index_entries(output_dir: str | Path) -> list[dict[str, Any]]:
    """INDEX.json entries, rebuilding the index when it is missing or unreadable."""
    idx_file = index_path(output_dir)
    if idx_file.exists():
        try:
            data = json.loads(idx_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # ValueError covers JSONDecodeError and bad UTF-8
            data = None
        packs = data.get("packs") if isinstance(data, dict) else None
        if isinstance(packs, list):
            return list(packs)
    return list(rebuild_index(output_dir).get("packs") or [])


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
    entries = _index_entries(output_dir)

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


class PackStatusConflict(Exception):
    """The pack's status is not one the caller expected (someone else changed it first)."""

    def __init__(self, pack_id: str, current: str, note: str, holder_at: str) -> None:
        super().__init__(f"pack {pack_id} is {current}")
        self.pack_id, self.current, self.note, self.at = pack_id, current, note, holder_at


# A claim succeeds only from these: claiming a claimed or in-progress pack would let two agents
# fix the same thing, each believing it owns it.
CLAIMABLE_STATUSES = frozenset({"open", "unverified", "obsolete", "wontfix", "resolved"})


def update_pack_status(
    output_dir: str | Path,
    pack_id: str,
    status: str,
    note: str = "",
    *,
    public_base_url: str = "",
    repair_updates: dict[str, Any] | None = None,
    verification_updates: dict[str, Any] | None = None,
    expect: Any = None,
) -> dict[str, Any] | None:
    """Persist a lifecycle status on the pack YAML and refresh INDEX.json.

    With *expect* (a collection of statuses) the change is compare-and-set: if the pack's current
    status is not in it, nothing is written and ``PackStatusConflict`` is raised. Returns the
    updated pack dict, or ``None`` when the pack does not exist. Raises ``ValueError`` for an
    unknown status.
    """
    if status not in PACK_STATUSES:
        raise ValueError(f"invalid pack status {status!r}; expected one of {PACK_STATUSES}")
    # Locked end to end so two writers on one pack can't interleave, and the index is patched in
    # place: a full rebuild per claim took ~12 s of GIL-bound parsing and queued every other agent.
    with pack_lock(output_dir):
        data = load_pack(output_dir, pack_id)
        if data is None:
            return None
        pack = AgentCodingPack.from_dict(data, fallback_id=pack_id)
        current = pack.status or DEFAULT_PACK_STATUS
        if expect is not None and current not in expect:
            raise PackStatusConflict(pack_id, current, pack.status_note, pack.status_updated_at)
        pack.apply_status(status, (note or "")[:2000])
        if repair_updates:
            pack.repair.update(repair_updates)
        if verification_updates:
            pack.verification.update(verification_updates)
        write_pack(pack, output_dir, preserve_status=False)
        _upsert_index_entry(output_dir, pack, public_base_url=public_base_url)
    return pack.to_dict()


def snapshot_pack_lifecycle(output_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Capture non-default lifecycle state so a full re-emit can restore it."""
    snap: dict[str, dict[str, Any]] = {}
    for yaml_path in packs_dir(output_dir).glob("*.yaml"):
        data = _read_pack_yaml(yaml_path)
        if not data or not data.get("work_item_id"):
            continue
        pack = AgentCodingPack.from_dict(data)
        if pack.status == DEFAULT_PACK_STATUS and not pack.status_history and not pack.repair.get("draft_pr_url"):
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
    if entry.get("kind") == "class_guard":
        # One guard closes many findings here, so it leads the bundle for every repo it covers (#54).
        primary = True
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
    elif status == "unverified":
        score -= 0.2  # a fix exists; what's missing is the proof
    if entry.get("kind") == "class_guard":
        score += 1.0
    elif entry.get("class_parent") and not entry.get("class_exception"):
        score -= 0.1  # its guard task covers it
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
        ingest_packs_to_rag(packs, output_dir=output_dir)
    return packs


def _rekey(pack: AgentCodingPack, new_id: str) -> None:
    """Give *pack* another id, keeping the id-derived repair slug and pack URL in step."""
    pack.work_item_id = new_id
    pack.repair["slug"] = new_id.replace("pack-", "repair/", 1) if new_id.startswith("pack-") else f"repair/{new_id}"
    if pack.repair.get("pack_url"):
        pack.repair["pack_url"] = f"{str(pack.repair['pack_url']).rsplit('/', 1)[0]}/{new_id}"


def _status_rank(status: str) -> int:
    return _MERGE_INTO_STATUSES.index(status) if status in _MERGE_INTO_STATUSES else len(_MERGE_INTO_STATUSES)


def _merge_targets(entries: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Specialist packs a repeat may merge into, grouped by ``(repo, file_path)``.

    Packs closed as a duplicate are left out: the issue lives on in their keeper."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        if (e.get("layer") == "world_model" or e.get("kind") in _NO_TITLE_MATCH_KINDS or e.get("duplicate_of")
                or str(e.get("status") or DEFAULT_PACK_STATUS) not in _MERGE_INTO_STATUSES):
            continue
        out[(str(e.get("repo") or ""), str(e.get("file_path") or ""))].append(e)
    return out


def merge_into_existing(packs: list[AgentCodingPack], output_dir: str | Path) -> dict[str, str]:
    """Re-key each new pack that repeats an existing pack to that pack's id, in place.

    A repeat is in the same repo and file with a reworded title (``finding_match``), from a
    different run: within one run, near-identical titles in a file are often distinct bugs.
    Each existing pack absorbs at most one new pack, best match first, preferring the pack
    furthest along the lifecycle. Writing the re-keyed pack then carries that pack's status
    over (``write_pack``), so a claimed or wontfix issue stays claimed or wontfix instead of
    reappearing as a fresh open twin. Returns ``{new_id: existing_id}``.
    """
    targets = _merge_targets(_index_entries(output_dir))
    if not targets:
        return {}
    batch_ids = {p.work_item_id for p in packs}
    cands: list[tuple[float, int, int, str]] = []
    for i, p in enumerate(packs):
        if p.kind in _NO_TITLE_MATCH_KINDS or pack_file_path(output_dir, p.work_item_id).exists():
            continue  # an exact-id re-emit already updates its own pack
        run = str((p.provenance or {}).get("run_id") or "")
        loc = p.location or {}
        for e in targets.get(((p.repo or {}).get("name", ""), loc.get("file_path", "")), []):
            if e["id"] in batch_ids or (run and str(e.get("run_id") or "") == run):
                continue
            score = reworded_score((p.narrative or {}).get("title", ""), str(e.get("title") or ""),
                                   loc.get("line"), e.get("line"))
            if score > 0:
                cands.append((-score, _status_rank(str(e.get("status") or "")), i, e["id"]))
    merged: dict[str, str] = {}
    used_new: set[int] = set()
    used_old: set[str] = set()
    for _neg, _rank, i, eid in sorted(cands):
        if i in used_new or eid in used_old:
            continue
        used_new.add(i)
        used_old.add(eid)
        merged[packs[i].work_item_id] = eid
        _rekey(packs[i], eid)
    return merged


def link_recurrences(packs: list[AgentCodingPack], output_dir: str | Path) -> dict[str, str]:
    """Mark each new pack that repeats a *resolved* pack with ``governance.recurrence_of``.

    Resolved packs aren't merge targets (a comeback gets a fresh open pack), but the trust
    metrics (#52) need to know the fix didn't hold. Same match as dedupe (same repo and file,
    reworded title, different run), one resolved pack per new pack, best match first.
    Returns ``{new_id: resolved_id}``."""
    resolved: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in _index_entries(output_dir):
        if (e.get("status") == "resolved" and e.get("layer") != "world_model"
                and e.get("kind") not in _NO_TITLE_MATCH_KINDS):
            resolved[(str(e.get("repo") or ""), str(e.get("file_path") or ""))].append(e)
    if not resolved:
        return {}
    cands: list[tuple[float, int, str]] = []
    for i, p in enumerate(packs):
        if p.kind in _NO_TITLE_MATCH_KINDS or (p.governance or {}).get("recurrence_of"):
            continue
        run = str((p.provenance or {}).get("run_id") or "")
        loc = p.location or {}
        for e in resolved.get(((p.repo or {}).get("name", ""), loc.get("file_path", "")), []):
            if e["id"] == p.work_item_id or (run and str(e.get("run_id") or "") == run):
                continue
            score = reworded_score((p.narrative or {}).get("title", ""), str(e.get("title") or ""),
                                   loc.get("line"), e.get("line"))
            if score > 0:
                cands.append((-score, i, e["id"]))
    linked: dict[str, str] = {}
    used_new: set[int] = set()
    used_old: set[str] = set()
    for _neg, i, eid in sorted(cands):
        if i in used_new or eid in used_old:
            continue
        used_new.add(i)
        used_old.add(eid)
        if packs[i].governance is None:
            packs[i].governance = {}
        packs[i].governance["recurrence_of"] = eid
        linked[packs[i].work_item_id] = eid
    return linked


def dedupe_packs(
    output_dir: str | Path, *, dry_run: bool = False, public_base_url: str = "",
) -> list[dict[str, Any]]:
    """Close open packs that repeat another pack as ``wontfix`` ("duplicate of <id>").

    Same matching as ``merge_into_existing``: same repo and file, reworded title, different
    runs, title-bearing kinds only. In each file the keeper is the pack furthest along the
    lifecycle (in progress, claimed, wontfix, open), newest first; only open packs are closed,
    so nobody's claimed work is touched. Closed duplicates leave the RAG collection like any
    wontfix pack. Returns ``[{id, duplicate_of, title, keeper_status}]``.
    """
    entries = list(rebuild_index(output_dir, public_base_url=public_base_url).get("packs") or [])
    dupes: list[dict[str, Any]] = []
    for group in _merge_targets(entries).values():
        group = sorted(group, key=lambda e: str(e.get("detected_at") or ""), reverse=True)
        group.sort(key=lambda e: _status_rank(str(e.get("status") or "")))
        keepers: list[dict[str, Any]] = []
        for e in group:
            best, best_score = None, 0.0
            for k in keepers:
                if str(k.get("run_id") or "") == str(e.get("run_id") or ""):
                    continue
                sc = reworded_score(str(e.get("title") or ""), str(k.get("title") or ""), e.get("line"), k.get("line"))
                if sc > best_score:
                    best, best_score = k, sc
            if best is not None and str(e.get("status") or DEFAULT_PACK_STATUS) == "open":
                dupes.append({"id": e["id"], "duplicate_of": best["id"], "title": e.get("title", ""),
                              "keeper_status": best.get("status", "")})
            else:
                keepers.append(e)
    if dry_run or not dupes:
        return dupes
    closed: list[AgentCodingPack] = []
    done: list[dict[str, Any]] = []
    # Load, re-check and write under the lock claims take: the index snapshot above is stale by
    # now, and a pack claimed since then is someone's work, not a duplicate to close.
    with pack_lock(output_dir):
        for d in dupes:
            data = load_pack(output_dir, d["id"])
            if data is None:
                continue
            pack = AgentCodingPack.from_dict(data, fallback_id=d["id"])
            if (pack.status or DEFAULT_PACK_STATUS) != "open":
                continue
            pack.apply_status("wontfix", f"duplicate of {d['duplicate_of']}")
            pack.governance["duplicate_of"] = d["duplicate_of"]
            write_pack(pack, output_dir, preserve_status=False)
            closed.append(pack)
            done.append(d)
        rebuild_index(output_dir, public_base_url=public_base_url)
    if closed and pack_rag_ingest_enabled():
        try:
            ingest_packs_to_rag(closed, output_dir=output_dir)  # retires them (wontfix)
        except Exception as exc:  # noqa: BLE001 — the next emit re-syncs
            logger.warning("RAG retire of duplicate packs failed: %s", exc)
    return done


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
        packs.append(build_pack_from_finding(
            finding,
            repo_path=root,
            kind=kind,
            run_id=run_id,
            directly_changed=directly_changed,
            transitively_affected=transitively_affected,
            public_base_url=public_base_url,
        ))
    # Dedupe, recurrence and write_pack all read existing pack state: decide and write under one
    # lock, or two emits both see "new" and write twin packs, or an agent's claim landing in
    # between is overwritten. The index is patched, not rebuilt.
    with pack_lock(output_dir):
        merged = merge_into_existing(packs, output_dir)
        if merged:
            logger.info("%d of %d %s packs repeat an existing pack; reusing its id", len(merged), len(packs), kind)
        recurred = link_recurrences([p for p in packs if p.work_item_id not in merged.values()], output_dir)
        if recurred:
            logger.info("%d %s packs repeat a resolved pack (recurrence_of)", len(recurred), kind)
        for pack in packs:
            write_pack(pack, output_dir)
        upsert_index_entries(output_dir, packs, public_base_url=public_base_url)
    if ingest_rag and packs:
        ingest_packs_to_rag(packs, output_dir=output_dir)
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
        if file_path.startswith(f"{repo}/"):  # "repo/path.py": store the repo-relative path
            file_path = file_path[len(repo) + 1:]
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
    if file_path and not line:  # "repo/path/file.py:123": the line is not part of the path
        m_line = re.match(r"^(.*\.[A-Za-z0-9]+):(\d+)\s*$", file_path)
        if m_line:
            file_path, line = m_line.group(1), int(m_line.group(2))

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


_LINE_SUFFIX = re.compile(r":\d+\s*$")


def architecture_issue(rule_name: str, violating_path: str) -> str:
    """What makes two architecture violations the same issue across runs: the rule and the
    violating path without its line, so code moving within a file keeps one pack."""
    return f"{rule_name}|{_LINE_SUFFIX.sub('', ' '.join((violating_path or '').split()))}"


def architecture_pack_id(repo: str, rule_name: str, violating_path: str) -> str:
    """The stable pack id for an architecture issue, the same on every run that reports it."""
    digest = hashlib.sha1(architecture_issue(rule_name, violating_path).encode()).hexdigest()[:10]
    repo_slug = re.sub(r"[^a-z0-9]+", "-", (repo or "repo").lower()).strip("-")[:32]
    return f"pack-{repo_slug}-arch-{digest}"


def architecture_findings(violations: list[RuleViolation]) -> list[Finding]:
    """One finding per architecture issue, each with its stable pack id as ``Finding.id``.
    Violations of one issue (the same rule and file at several lines) share it, evidence merged."""
    by_issue: dict[str, Finding] = {}
    for v in violations:
        f = finding_from_violation(v)
        key = architecture_issue(v.rule_name, v.violating_path)
        if key in by_issue:
            kept = by_issue[key]
            if f.evidence and f.evidence not in (kept.evidence or ""):
                kept.evidence = f"{kept.evidence}\n{f.evidence}" if kept.evidence else f.evidence
                kept.description = kept.evidence
            continue
        f.id = architecture_pack_id(f.repo, v.rule_name, v.violating_path)
        by_issue[key] = f
    return list(by_issue.values())


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
    """One pack per architecture issue, under an id stable across runs, so a re-report updates
    the same pack (keeping its status) instead of adding another. Closing the ones a run no
    longer reports is ``pack_gc.close_unreported_architecture``."""
    findings = architecture_findings(violations)[: max(1, limit)]
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
    include_ecosystem: bool = True,
    include_memory: bool = True,
    repos: list[str] | None = None,
) -> list[AgentCodingPack]:
    """Compressed world model: ecosystem overview + per-repo briefings + architecture memory.

    Pass ``repos=["argus-cortex"]`` (and usually ``include_ecosystem=False``,
    ``include_memory=False``) for a lightweight single-repo refresh after a merge.
    """
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
    repo_filter = {n.strip().lower() for n in (repos or []) if n and n.strip()}

    if include_ecosystem:
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
    if include_memory and mem_path.exists():
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
                if repo_filter and repo.name.lower() not in repo_filter:
                    continue
                try:
                    root = resolve_repo_path(repo)
                except Exception as exc:  # noqa: BLE001 — one unresolvable repo must not stop the rest
                    logger.warning("skipping %s: can't resolve its path: %s", repo.name, exc)
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
        ingest_packs_to_rag(packs, output_dir=output_dir)
    return packs


def refresh_pack_rag(output_dir: str | Path, updated: dict[str, Any] | None) -> dict[str, Any] | None:
    """Re-send (or retire, once closed) one pack's RAG doc after its status changed.

    Every status writer calls this: the RAG title carries the status, and closed packs must
    leave the collection. Best-effort; never raises."""
    if updated is None or not pack_rag_ingest_enabled():
        return None
    try:
        return ingest_packs_to_rag([AgentCodingPack.from_dict(updated)], output_dir=output_dir)
    except Exception as exc:  # noqa: BLE001 — the status change stands; the next emit re-syncs
        logger.warning("pack status RAG refresh failed for %s: %s", updated.get("id") or updated.get("work_item_id"), exc)
        return {"ingested": False, "error": str(exc)[:300]}


def ingest_packs_to_rag(packs: list[AgentCodingPack], *, output_dir: str | Path | None = None,
                        full: bool = False) -> dict[str, Any]:
    """Upsert slim pack summaries into the audit RAG collection for agent retrieval.

    Posted per repo, so each request carries ``repo:<name>`` in its tags (rag-ingest keeps
    tags, title and url per chunk, but drops other metadata). Closed packs (resolved, wontfix)
    are retired from the collection instead of re-sent, so retrieval surfaces open work. With
    ``full=True`` (``packs`` is every pack on disk), packs sent earlier that no longer exist are
    retired too. The record of what was sent is ``rag_ingested.json`` (scope ``packs``)."""
    from auditor.rag_client import post_documents, record_sent, recorded_ids, retire

    by_repo: dict[str, list[dict[str, Any]]] = {}
    closed: set[str] = set()
    # Two findings merged into one pack share its id; rag-ingest rejects a request that names a doc
    # twice (the whole repo's batch failed). The last one wins, as it did on disk.
    packs = list({p.work_item_id: p for p in packs}.values())
    for pack in packs:
        doc_id = f"agent-pack::{pack.work_item_id}"
        if (pack.status or DEFAULT_PACK_STATUS) not in ACTIVE_PACK_STATUSES:
            closed.add(doc_id)
            continue
        repo = (pack.repo or {}).get("name", "") or "ecosystem"
        sev = (pack.priority or {}).get("severity", "")
        title = (pack.narrative or {}).get("title", pack.work_item_id)
        by_repo.setdefault(repo, []).append({
            "id": doc_id,
            "title": f"{repo} · {title} [{sev} · {pack.status or DEFAULT_PACK_STATUS} · {pack.layer()}]",
            "url": (pack.repair or {}).get("pack_url") or "",
            "text": pack.to_rag_text(),
        })
    if not by_repo and not closed and not full:
        return {"ingested": False, "skipped": True, "count": 0}
    result: dict[str, Any] = {"ingested": True, "skipped": False, "count": 0, "retired": 0}
    sent: set[str] = set()
    for repo, docs in sorted(by_repo.items()):
        # each accepted batch is recorded as it lands, so a later failure can't orphan it
        r = post_documents(docs, tags=["kind:agent_coding_pack", f"repo:{repo}"],
                           on_sent=lambda batch: record_sent(output_dir, "packs", batch))
        result["count"] += r.get("count", 0)
        if not r.get("ingested"):
            return {**result, **{k: v for k, v in r.items() if k != "count"}, "ingested": False}
        sent |= {d["id"] for d in docs}
    gone = set(closed)
    if full:
        gone |= recorded_ids(output_dir, "packs") - sent
    # Closed packs may predate the record (sent before it existed), so they're deleted
    # whether or not it lists them; deleting an id that isn't there is harmless.
    d = retire(output_dir, "packs", gone)
    result["retired"] = d.get("count", 0)
    if not d.get("deleted"):
        result["retire_error"] = d.get("error")
    return result
