from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SEVERITY_LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class CodeUnit:
    id: str
    repo: str
    file_path: str
    symbol: str
    kind: str
    language: str
    start_line: int
    end_line: int
    ast_features: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    raw_text_hash: str = ""


@dataclass(slots=True)
class Finding:
    id: str
    type: str
    severity: str
    repo: str
    file_path: str
    line: int
    title: str
    description: str
    evidence: str
    recommendation: str
    source: str
    detected_at: str = field(default_factory=utc_now_iso)

    def validate(self) -> None:
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(f"Invalid severity: {self.severity}")
        if self.line < 0:
            raise ValueError("line must be >= 0")


@dataclass(slots=True)
class RuleViolation:
    rule_name: str
    violating_path: str
    entities: list[str]
    evidence: str
    severity: str = "HIGH"
    detected_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class PatternProposal:
    domain: str
    candidates: list[str]
    prevalence_by_repo: dict[str, int]
    risk_assessment: str
    recommendation: str
    status: str = "pending"


@dataclass(slots=True)
class BranchDiff:
    """Files that differ between two branches in a local repository."""
    base: str               # e.g. "main" or "dev"
    head: str               # e.g. "dev" or "feature/x"
    changed_files: frozenset[str]  # repo-relative paths


@dataclass(slots=True)
class ScanContext:
    """Result of a branch-aware repository scan."""
    files: list           # list[Path] — all scannable source files
    checked_out_branch: str
    diffs: list           # list[BranchDiff], ordered: feature→dev first, then dev→main/master


@dataclass(slots=True)
class CopilotResponse:
    answer: str
    supporting_entities: list[str]
    impacted_repos: list[str]
    confidence: float
    citations: list[str]


# ── Diff-audit contracts ─────────────────────────────────────────────────────

@dataclass(slots=True)
class DiffHunk:
    """A single @@ hunk within a unified diff."""
    file_path: str            # repo-relative canonical path (b-side)
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    raw_hunk: str             # hunk text (header + body lines joined by \n)
    added_lines: list[int]    # 1-indexed new-file line numbers that were added
    removed_lines: list[int]  # 1-indexed old-file line numbers that were removed


@dataclass(slots=True)
class FileDiff:
    """Diff for a single file (all hunks collected)."""
    old_path: str
    new_path: str
    hunks: list[DiffHunk]
    is_new_file: bool = False
    is_deleted: bool = False
    is_rename: bool = False


@dataclass(slots=True)
class ImpactResult:
    """Files directly changed plus those that transitively import them."""
    directly_changed: frozenset[str]
    transitively_affected: frozenset[str]


@dataclass(slots=True)
class DiffAuditResult:
    repo: str
    file_diffs: list[FileDiff]
    impact: ImpactResult
    units_analyzed: list[CodeUnit]
    findings: list[Finding]
    generated_at: str = field(default_factory=utc_now_iso)
