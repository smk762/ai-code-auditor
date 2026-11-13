"""Mithrandir shared status-event envelope + auditor payload (schema_version 1).

LATEST.json is one contributor to the morning broadcast (source=ai-code-auditor).
It does not claim exclusive ownership of companion delivery — mithrandir merges
envelope events from energy, thermal, auditor, etc.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = 1
SOURCE = "ai-code-auditor"

EndedReason = Literal["completed", "time_budget", "escalation"]
DraftPrAction = Literal["review", "merge_ready", "blocked"]
ReviewIntensity = Literal["off", "standard", "high", "max"]
StatusSeverity = Literal["info", "notice", "warning", "critical"]

_SEV_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


@dataclass(slots=True)
class TopFinding:
    repo: str
    severity: str
    title: str
    pr_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "repo": self.repo,
            "severity": self.severity,
            "title": self.title,
        }
        if self.pr_url:
            d["pr_url"] = self.pr_url
        return d


@dataclass(slots=True)
class FindingsSummary:
    confirmed: int = 0
    dismissed: int = 0
    manual_review: int = 0
    top: list[TopFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "dismissed": self.dismissed,
            "manual_review": self.manual_review,
            "top": [t.to_dict() for t in self.top[:3]],
        }


@dataclass(slots=True)
class CodeReviewSummary:
    intensity: str
    status: str
    findings_count: int = 0
    fix_rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DraftPr:
    url: str
    title: str
    severity: str
    repo: str
    action: DraftPrAction = "review"
    code_review: CodeReviewSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "url": self.url,
            "title": self.title,
            "severity": self.severity,
            "repo": self.repo,
            "action": self.action,
        }
        if self.code_review is not None:
            d["code_review"] = self.code_review.to_dict()
        return d


@dataclass(slots=True)
class DecisionNeeded:
    id: str
    summary: str
    repo: str = ""
    pr_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "summary": self.summary}
        if self.repo:
            d["repo"] = self.repo
        if self.pr_url:
            d["pr_url"] = self.pr_url
        return d


@dataclass(slots=True)
class RestWindowInfo:
    start: str
    end: str
    timezone: str = "UTC"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _max_finding_severity(findings: FindingsSummary) -> str:
    best = "INFO"
    for t in findings.top:
        label = (t.severity or "INFO").upper()
        if _SEV_RANK.get(label, 0) > _SEV_RANK.get(best, 0):
            best = label
    return best


def derive_requires_ack(
    ended_reason: EndedReason,
    decisions_needed: list[DecisionNeeded] | None = None,
) -> bool:
    """True for escalations / pending human decisions; false for clean completed runs."""
    if ended_reason == "escalation":
        return True
    return bool(decisions_needed)


def derive_severity(
    ended_reason: EndedReason,
    findings: FindingsSummary | None = None,
    decisions_needed: list[DecisionNeeded] | None = None,
    draft_prs: list[DraftPr] | None = None,
) -> StatusSeverity:
    """Honest severity so quiet hours are not interrupted with noncritical updates."""
    findings = findings or FindingsSummary()
    decisions_needed = decisions_needed or []
    draft_prs = draft_prs or []
    top_sev = _max_finding_severity(findings)

    if ended_reason == "escalation":
        return "critical"
    if decisions_needed:
        return "critical" if top_sev == "CRITICAL" else "warning"
    if top_sev == "CRITICAL":
        return "warning"
    if top_sev == "HIGH" or any(p.action == "blocked" for p in draft_prs):
        return "notice"
    if findings.confirmed or draft_prs or ended_reason == "time_budget":
        return "notice"
    return "info"


def derive_quiet_exempt(severity: StatusSeverity, requires_ack: bool) -> bool:
    """Only ack-required warning/critical may break quiet hours."""
    return bool(requires_ack and severity in ("warning", "critical"))


@dataclass(slots=True)
class MithrandirStatus:
    """Auditor instance of the shared mithrandir status-event envelope."""

    run_id: str
    generated_at: str
    ended_reason: EndedReason
    summary_one_liner: str
    speakable: bool
    latest_url: str
    latest_md_url: str
    window: RestWindowInfo
    body_md: str = ""
    event_id: str = ""
    expires_at: str | None = None
    channels_hint: list[str] | None = None
    repos: list[str] = field(default_factory=list)
    scanner_counts: dict[str, int] = field(default_factory=dict)
    findings: FindingsSummary = field(default_factory=FindingsSummary)
    draft_prs: list[DraftPr] = field(default_factory=list)
    decisions_needed: list[DecisionNeeded] = field(default_factory=list)
    worker_stats: dict[str, Any] = field(default_factory=dict)
    cost_usd_est: float = 0.0
    next_rest_window: str = ""
    schema_version: int = SCHEMA_VERSION
    source: str = SOURCE
    requires_ack: bool | None = None
    severity: StatusSeverity | None = None
    quiet_exempt: bool | None = None
    # TTS-safe blocked-PR sentence(s) from auditor.pr_intel; spoken before the next window.
    pr_brief: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            object.__setattr__(self, "event_id", f"auditor-rest-{self.run_id}")
        if self.requires_ack is None:
            object.__setattr__(
                self,
                "requires_ack",
                derive_requires_ack(self.ended_reason, list(self.decisions_needed)),
            )
        if self.severity is None:
            object.__setattr__(
                self,
                "severity",
                derive_severity(
                    self.ended_reason,
                    self.findings,
                    list(self.decisions_needed),
                    list(self.draft_prs),
                ),
            )
        if self.quiet_exempt is None:
            object.__setattr__(
                self,
                "quiet_exempt",
                derive_quiet_exempt(self.severity or "info", bool(self.requires_ack)),
            )

    def to_dict(self) -> dict[str, Any]:
        one = (self.summary_one_liner or "").strip()
        if len(one) > 120:
            one = one[:117].rstrip() + "..."
        requires_ack = (
            bool(self.requires_ack)
            if self.requires_ack is not None
            else derive_requires_ack(self.ended_reason, list(self.decisions_needed))
        )
        severity: StatusSeverity = self.severity or derive_severity(
            self.ended_reason,
            self.findings,
            list(self.decisions_needed),
            list(self.draft_prs),
        )
        quiet_exempt = (
            bool(self.quiet_exempt)
            if self.quiet_exempt is not None
            else derive_quiet_exempt(severity, requires_ack)
        )
        out: dict[str, Any] = {
            # ── shared envelope ──────────────────────────────────────────
            "schema_version": self.schema_version,
            "source": self.source,
            "event_id": self.event_id or f"auditor-rest-{self.run_id}",
            "severity": severity,
            "generated_at": self.generated_at,
            "speakable": self.speakable,
            "summary_one_liner": one,
            "body_md": self.body_md,
            "requires_ack": requires_ack,
            "quiet_exempt": quiet_exempt,
            # ── auditor payload ──────────────────────────────────────────
            "latest_url": self.latest_url,
            "latest_md_url": self.latest_md_url,
            "run_id": self.run_id,
            "window": self.window.to_dict(),
            "ended_reason": self.ended_reason,
            "repos": list(self.repos),
            "scanner_counts": dict(self.scanner_counts),
            "findings": self.findings.to_dict(),
            "draft_prs": [p.to_dict() for p in self.draft_prs],
            "decisions_needed": [d.to_dict() for d in self.decisions_needed],
            "worker_stats": dict(self.worker_stats),
            "cost_usd_est": float(self.cost_usd_est),
            "next_rest_window": self.next_rest_window,
        }
        if self.expires_at:
            out["expires_at"] = self.expires_at
        if self.channels_hint:
            out["channels_hint"] = list(self.channels_hint)
        if self.pr_brief:
            out["pr_brief"] = self.pr_brief
        return out


def validate_status_dict(data: dict[str, Any]) -> list[str]:
    """Return a list of validation errors (empty = ok)."""
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for key in (
        "generated_at",
        "source",
        "event_id",
        "severity",
        "summary_one_liner",
        "speakable",
        "body_md",
        "requires_ack",
        "quiet_exempt",
        "latest_url",
        "latest_md_url",
        "run_id",
        "window",
        "ended_reason",
        "findings",
        "draft_prs",
    ):
        if key not in data:
            errors.append(f"missing field: {key}")
    if data.get("source") != SOURCE:
        errors.append(f'source must be "{SOURCE}"')
    one = data.get("summary_one_liner", "")
    if isinstance(one, str) and len(one) > 120:
        errors.append("summary_one_liner exceeds 120 chars")
    if data.get("severity") not in ("info", "notice", "warning", "critical"):
        errors.append("severity invalid")
    if not isinstance(data.get("requires_ack"), bool):
        errors.append("requires_ack must be bool")
    if not isinstance(data.get("quiet_exempt"), bool):
        errors.append("quiet_exempt must be bool")
    reason = data.get("ended_reason")
    if reason not in ("completed", "time_budget", "escalation"):
        errors.append("ended_reason invalid")
    findings = data.get("findings") or {}
    top = findings.get("top") if isinstance(findings, dict) else None
    if isinstance(top, list) and len(top) > 3:
        errors.append("findings.top must have at most 3 items")
    for pr in data.get("draft_prs") or []:
        if not isinstance(pr, dict):
            errors.append("draft_prs entries must be objects")
            continue
        if pr.get("action") not in ("review", "merge_ready", "blocked"):
            errors.append(f"draft_prs.action invalid: {pr.get('action')!r}")
    return errors
