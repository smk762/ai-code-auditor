"""Mission prompts and structured worker task definitions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

MissionType = Literal["security", "bughunt", "invariants", "refactor", "pr_review", "pr_fix"]


@dataclass(slots=True)
class WorkerTask:
    id: str
    mission: MissionType
    title: str
    repo: str
    repo_path: str
    prompt: str
    intensity: str = "high"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkerResult:
    task_id: str
    status: Literal["COMPLETED", "PARTIAL", "FAILED", "ESCALATE"]
    summary: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    patch_notes: str = ""
    escalate_reason: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "findings": self.findings,
            "patch_notes": self.patch_notes,
            "escalate_reason": self.escalate_reason,
        }


SECURITY_SYSTEM = """You are a security Hunter worker. Find real, reachable vulnerabilities.
Return ONLY JSON:
{"status":"COMPLETED|PARTIAL|FAILED|ESCALATE","summary":"...","findings":[{"category":"...","file":"...","line":0,"symbol":"...","severity":"HIGH","title":"...","call_path":[],"missing_evidence":"..."}],"escalate_reason":""}
Cap findings at 5. Cite concrete files/symbols. Do not invent issues."""

BUGHUNT_SYSTEM = """You are a bug-hunt worker for logic, concurrency, and error-path bugs.
Return ONLY JSON with the same schema as the security worker. Cap at 5 findings."""

INVARIANTS_SYSTEM = """You are an architecture invariants worker. Check architecture_rules /
service-role / forbidden cross-repo calls. Return ONLY JSON with findings schema.
Cap at 5."""

REFACTOR_SYSTEM = """You are a behavior-preserving refactor worker. Propose small safe cleanups only.
Return ONLY JSON: {"status":"...","summary":"...","findings":[],"patch_notes":"...","escalate_reason":""}"""

PR_REVIEW_SYSTEM = """You are an independent PR code reviewer (not the author). Review the diff.
Intensity levels: standard=bugs/regressions/tests; high=+security+call paths; max=+skeptic+architecture.
Return ONLY JSON:
{"status":"COMPLETED|ESCALATE","summary":"...","verdict":"clean|needs_fix|escalate","findings":[{"file":"...","title":"...","severity":"..."}],"escalate_reason":""}"""

PR_FIX_SYSTEM = """You are a PR fix worker. Apply the smallest patch addressing reviewer findings.
Return ONLY JSON: {"status":"...","summary":"...","findings":[],"patch_notes":"...","escalate_reason":""}"""


def system_for(mission: MissionType) -> str:
    return {
        "security": SECURITY_SYSTEM,
        "bughunt": BUGHUNT_SYSTEM,
        "invariants": INVARIANTS_SYSTEM,
        "refactor": REFACTOR_SYSTEM,
        "pr_review": PR_REVIEW_SYSTEM,
        "pr_fix": PR_FIX_SYSTEM,
    }[mission]


def parse_worker_json(raw: str, task_id: str) -> WorkerResult:
    text = raw.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return WorkerResult(task_id=task_id, status="FAILED", summary="no JSON", raw=raw)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return WorkerResult(task_id=task_id, status="FAILED", summary="invalid JSON", raw=raw)
    status = str(data.get("status") or "PARTIAL").upper()
    if status not in ("COMPLETED", "PARTIAL", "FAILED", "ESCALATE"):
        status = "PARTIAL"
    return WorkerResult(
        task_id=task_id,
        status=status,  # type: ignore[arg-type]
        summary=str(data.get("summary") or ""),
        findings=list(data.get("findings") or [])[:10],
        patch_notes=str(data.get("patch_notes") or ""),
        escalate_reason=str(data.get("escalate_reason") or ""),
        raw=raw,
    )


def build_tasks_from_scanners(
    *,
    repo: str,
    repo_path: Path,
    scanner_findings: list[Any],
    architecture_rules_path: Path,
    max_tasks: int = 12,
    awareness_brief: str = "",
) -> list[WorkerTask]:
    """Conductor planning heuristic (deterministic) — LLM planner can replace later."""
    tasks: list[WorkerTask] = []
    awareness_block = (
        f"\nOpen PRs/issues (do not duplicate):\n{awareness_brief}\n"
        if awareness_brief
        else ""
    )
    if scanner_findings:
        sample = scanner_findings[:15]
        brief = "\n".join(
            f"- [{getattr(f, 'severity', '?')}] {getattr(f, 'file_path', '')}:{getattr(f, 'line', 0)} {getattr(f, 'title', '')}"
            for f in sample
        )
        tasks.append(
            WorkerTask(
                id=f"{repo}-security-001",
                mission="security",
                title=f"Security pass on {repo}",
                repo=repo,
                repo_path=str(repo_path),
                intensity="max",
                prompt=(
                    f"Repo: {repo} at {repo_path} (branch:dev)\n"
                    f"Scanner hits to triage and deepen:\n{brief}\n"
                    f"{awareness_block}"
                    "Confirm reachable issues; dismiss false positives and anything already covered by an open PR/issue."
                ),
            )
        )
    tasks.append(
        WorkerTask(
            id=f"{repo}-bughunt-001",
            mission="bughunt",
            title=f"Bug hunt on {repo}",
            repo=repo,
            repo_path=str(repo_path),
            intensity="high",
            prompt=(
                f"Repo: {repo} at {repo_path} (branch:dev). "
                f"Hunt logic/concurrency/error-path bugs in critical modules."
                f"{awareness_block}"
            ),
        )
    )
    rules_text = ""
    if architecture_rules_path.exists():
        rules_text = architecture_rules_path.read_text(encoding="utf-8")[:4000]
    tasks.append(
        WorkerTask(
            id=f"{repo}-invariants-001",
            mission="invariants",
            title=f"Architecture invariants for {repo}",
            repo=repo,
            repo_path=str(repo_path),
            intensity="high",
            prompt=(
                f"Repo: {repo}. Architecture rules excerpt:\n{rules_text}\n"
                f"Check for violations involving this repo.{awareness_block}"
            ),
        )
    )
    return tasks[:max_tasks]
