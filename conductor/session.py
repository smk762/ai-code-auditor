"""Night conductor session loop: plan → workers → review → draft PRs → wrap-up."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auditor.agent_pack import (
    emit_packs_from_worker_results,
    pack_rag_ingest_enabled,
    stable_pack_id,
)
from auditor.config import load_ecosystem_config
from conductor.code_review import review_draft_pr
from conductor.draft_pr import create_draft_pr, finding_to_pr_body
from conductor.github_awareness import (
    RepoAwareness,
    fetch_repo_awareness,
    filter_findings_against_awareness,
    overlap_reason,
)
from conductor.mirrors import sync_mirrors
from conductor.missions import WorkerResult, WorkerTask, build_tasks_from_scanners
from conductor.publish import public_base_url
from conductor.rest_window import (
    load_rest_windows,
    next_window_start_label,
    should_freeze_launches,
)
from conductor.scanners import filter_by_dispositions, load_dispositions, run_scanners
from conductor.schemas import (
    DecisionNeeded,
    DraftPr,
    FindingsSummary,
    RestWindowInfo,
)
from conductor.workers import run_workers_parallel, write_worker_handoff
from conductor.wrapup import build_status, top_findings_from_worker_results, wrap_up

logger = logging.getLogger(__name__)


def _autonomy(repo: str, pack_id: str, file_path: str, title: str, output_dir: str | Path):
    """Trust-gated autonomy's answer for this PR (#61). The pack's class when the classifier
    has set one, else the repo's ``unclassified`` scope."""
    from auditor.agent_pack import load_pack
    from auditor.autonomy import decide

    cls = (((load_pack(output_dir, pack_id) or {}).get("governance") or {}).get("finding_class") or {})
    return decide(repo, str(cls.get("id") or "") if isinstance(cls, dict) else "", paths=[file_path],
                  title=title, output_dir=output_dir)


@dataclass
class SessionState:
    run_id: str
    tasks_done: list[WorkerResult] = field(default_factory=list)
    draft_prs: list[DraftPr] = field(default_factory=list)
    draft_pr_by_pack: dict[str, str] = field(default_factory=dict)
    # (repo, finding title) -> real draft PR url, so a top finding links only its own PR.
    draft_pr_by_finding: dict[tuple[str, str], str] = field(default_factory=dict)
    decisions: list[DecisionNeeded] = field(default_factory=list)
    scanner_counts: dict[str, int] = field(default_factory=dict)
    repos: list[str] = field(default_factory=list)
    confirmed: int = 0
    dismissed: int = 0
    ended_reason: str = "completed"
    cost_usd_est: float = 0.0
    # PRs trust-gated autonomy held back at `report` (#61): {repo, title, scope, reason}
    autonomy_held: list[dict[str, str]] = field(default_factory=list)


def run_rest_session(
    *,
    dry_run: bool = False,
    force: bool = False,
    output_dir: str | None = None,
    max_workers: int = 4,
    enable_draft_prs: bool = True,
    enable_pr_review: bool = True,
    send_webhook: bool = True,
    ingest_rag: bool = True,
) -> dict[str, Any]:
    """Primary entry: one rest-window conductor session."""
    cfg = load_ecosystem_config()
    rest = load_rest_windows()
    out = Path(output_dir or cfg.output_dir)
    run_id = f"rest-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    state = SessionState(run_id=run_id)

    if not force and should_freeze_launches(rest) and not dry_run:
        # Outside window or already in wrap-up — still allow explicit force/dry-run.
        logger.warning("Outside active launch window; use --force to run anyway")
        window = RestWindowInfo(
            start=rest.start.strftime("%H:%M"),
            end=rest.end.strftime("%H:%M"),
            timezone=rest.timezone,
        )
        status = build_status(
            run_id=run_id,
            ended_reason="time_budget",
            window=window,
            repos=[r.name for r in cfg.repos],
            scanner_counts={},
            findings=FindingsSummary(),
            draft_prs=[],
            decisions_needed=[
                DecisionNeeded(
                    id="outside-window",
                    summary="Rest session skipped: outside launch window (use --force).",
                )
            ],
            worker_stats={"tasks": 0, "completed": 0, "failed": 0, "escalated": 0},
            next_rest_window=next_window_start_label(rest),
            summary_one_liner="Rest session skipped: outside launch window.",
        )
        return wrap_up(
            status,
            output_dir=out,
            send_webhook=send_webhook,
            ingest_rag=ingest_rag,
        )

    # Auto-sync mirrors + checkout dev before scanning.
    logger.info("Syncing repo mirrors (branch=dev)…")
    sync_results = sync_mirrors()
    for sr in sync_results:
        if not sr.ok:
            logger.warning("mirror sync failed: %s — %s", sr.name, sr.error)

    arch_rules = Path("config/architecture_rules.yaml")
    dispositions = load_dispositions()
    pending_tasks: list[WorkerTask] = []
    awareness_by_repo: dict[str, RepoAwareness] = {}

    for repo in cfg.repos:
        state.repos.append(repo.name)
        repo_path = Path(repo.path)
        if not repo_path.exists():
            logger.warning("repo path missing (configure local mirrors): %s", repo_path)
            state.decisions.append(
                DecisionNeeded(
                    id=f"missing-{repo.name}",
                    summary=f"Repo path missing for {repo.name}",
                    repo=repo.name,
                )
            )
            continue

        awareness = fetch_repo_awareness(repo_path, repo_slug=f"smk762/{repo.name}")
        awareness_by_repo[repo.name] = awareness

        scan_out = out / "security-out" / repo.name
        scan = run_scanners(repo_path, scan_out)
        for k, v in scan.counts.items():
            state.scanner_counts[k] = state.scanner_counts.get(k, 0) + v
        kept = filter_by_dispositions(scan.findings, dispositions)
        kept, skipped_open = filter_findings_against_awareness(kept, awareness)
        state.dismissed += max(0, len(scan.findings) - len(kept))
        if skipped_open:
            logger.info(
                "%s: skipped %d findings covered by open PR/issue",
                repo.name,
                len(skipped_open),
            )
        tasks = build_tasks_from_scanners(
            repo=repo.name,
            repo_path=repo_path,
            scanner_findings=kept,
            architecture_rules_path=arch_rules,
            max_tasks=rest.max_tasks_per_session,
            awareness_brief=awareness.brief(),
        )
        pending_tasks.extend(tasks)

    pending_tasks = pending_tasks[: rest.max_tasks_per_session]
    handoff_dir = out / "conductor" / run_id

    def _cancel() -> bool:
        # Same bypass as the launch gate above: a forced run or a dry-run ignores the window.
        return (not force) and (not dry_run) and should_freeze_launches(rest)

    # Conductor sits idle while workers run.
    results = run_workers_parallel(
        pending_tasks,
        max_workers=max_workers,
        dry_run=dry_run,
        should_cancel=_cancel,
    )
    by_id = {r.task_id: r for r in results}
    for task in pending_tasks:
        result = by_id.get(task.id) or WorkerResult(task_id=task.id, status="FAILED", summary="missing")
        write_worker_handoff(task, result, handoff_dir)
        state.tasks_done.append(result)
        if result.status == "ESCALATE":
            # A time-budget stop stays the session's reason: later tasks are only bookkept.
            if state.ended_reason != "time_budget":
                state.ended_reason = "escalation"
            state.decisions.append(
                DecisionNeeded(
                    id=result.task_id,
                    summary=result.escalate_reason or result.summary or "Worker escalated",
                    repo=task.repo,
                )
            )
        confirmed_findings = [f for f in result.findings if isinstance(f, dict)]
        state.confirmed += len(confirmed_findings)

        if enable_draft_prs and confirmed_findings and result.status in ("COMPLETED", "PARTIAL"):
            if _cancel() and not force:
                # Out of time for new PRs only: every finished task still gets its
                # handoff, escalation and confirmed count above.
                state.ended_reason = "time_budget"
                continue
            top = confirmed_findings[0]
            awareness = awareness_by_repo.get(task.repo) or RepoAwareness(repo=task.repo)
            clash = overlap_reason(
                title=str(top.get("title") or ""),
                file_path=str(top.get("file") or top.get("file_path") or ""),
                awareness=awareness,
            )
            if clash:
                # Still confirmed (counted above), just no new PR: not also a dismissal.
                logger.info("skip draft PR for %s: %s", task.repo, clash)
                continue
            # Pack id is deterministic from finding fields; emit_packs later writes the file.
            pack_id = stable_pack_id(
                repo=task.repo,
                file_path=str(top.get("file") or top.get("file_path") or ""),
                title=str(top.get("title") or task.title),
                source=f"conductor-{task.mission}",
                run_id=run_id,
                finding_id=str(top.get("id") or ""),
            )
            title = str(top.get("title") or task.title)[:70]
            gate = _autonomy(task.repo, pack_id, str(top.get("file") or top.get("file_path") or ""), title, out)
            if not gate.may_open_pr:
                # Still confirmed and still a pack; the scope hasn't earned a PR (#61).
                logger.info("no PR for %s (%s): %s", task.repo, gate.scope, gate.reason)
                state.autonomy_held.append({"repo": task.repo, "title": title, "scope": gate.scope,
                                            "reason": gate.reason})
                continue
            body = finding_to_pr_body(top, result.summary, pack_id=pack_id, repo_path=str(task.repo_path))
            pr_res = create_draft_pr(
                Path(task.repo_path),
                repo_name=task.repo,
                title=title,
                body=body,
                slug=f"{task.mission}-{task.id[-6:]}",
                severity=str(top.get("severity") or "MEDIUM"),
                base_branch="dev",
                dry_run=dry_run,
                draft=gate.draft,
            )
            if pr_res.draft:
                draft = pr_res.draft
                draft.autonomy = f"{gate.level}: {gate.reason}"
                if enable_pr_review:
                    draft = review_draft_pr(
                        draft,
                        mission=task.mission,
                        repo_path=task.repo_path,
                        dry_run=dry_run,
                        skip=_cancel() and not force,
                    )
                state.draft_prs.append(draft)
                if draft.url and not dry_run:  # dry-run URLs are placeholders
                    state.draft_pr_by_pack[pack_id] = draft.url
                    finding_title = str(top.get("title") or top.get("symbol") or "finding")[:160]
                    state.draft_pr_by_finding[(task.repo, finding_title)] = draft.url
            elif pr_res.error:
                state.decisions.append(
                    DecisionNeeded(
                        id=f"pr-{task.id}",
                        summary=f"Draft PR failed: {pr_res.error[:120]}",
                        repo=task.repo,
                    )
                )

    if _cancel() and state.ended_reason == "completed":
        state.ended_reason = "time_budget"

    worker_dicts = []
    for t in pending_tasks:
        r = by_id.get(t.id)
        if r:
            d = r.to_dict()
            d["repo"] = t.repo
            worker_dicts.append(d)

    findings = FindingsSummary(
        confirmed=state.confirmed,
        dismissed=state.dismissed,
        manual_review=len([d for d in state.decisions]),
        top=top_findings_from_worker_results(worker_dicts),
    )
    # Attach a PR url only to the finding that PR was opened for (never a dry-run placeholder).
    for tf in findings.top:
        url = state.draft_pr_by_finding.get((tf.repo, tf.title))
        if url:
            tf.pr_url = url

    window = RestWindowInfo(
        start=rest.start.strftime("%H:%M"),
        end=rest.end.strftime("%H:%M"),
        timezone=rest.timezone,
    )
    _refresh_pr_intel(out)
    status = build_status(
        run_id=run_id,
        ended_reason=state.ended_reason,
        window=window,
        repos=state.repos,
        scanner_counts=state.scanner_counts,
        findings=findings,
        draft_prs=state.draft_prs,
        decisions_needed=state.decisions,
        worker_stats={
            "tasks": len(state.tasks_done),
            "completed": sum(1 for r in state.tasks_done if r.status == "COMPLETED"),
            "failed": sum(1 for r in state.tasks_done if r.status == "FAILED"),
            "escalated": sum(1 for r in state.tasks_done if r.status == "ESCALATE"),
            "autonomy_held": state.autonomy_held,
        },
        cost_usd_est=state.cost_usd_est,
        next_rest_window=next_window_start_label(rest),
    )

    packs = emit_packs_from_worker_results(
        output_dir=out,
        run_id=run_id,
        tasks=pending_tasks,
        results_by_id=by_id,
        # Only the pack the draft PR targets is in_progress (not every pack in the repo).
        draft_pr_by_pack=state.draft_pr_by_pack,
        public_base_url=public_base_url(),
        ingest_rag=pack_rag_ingest_enabled(ingest_rag),
    )
    logger.info("emitted %d agent coding packs for run %s", len(packs), run_id)

    wrapped = wrap_up(
        status,
        output_dir=out,
        send_webhook=send_webhook,
        ingest_rag=ingest_rag,
    )
    wrapped["agent_packs"] = {
        "count": len(packs),
        "ids": [p.work_item_id for p in packs],
        "index": "agent_packs/INDEX.json",
        "list_url": f"{public_base_url()}/audit/packs",
    }
    return wrapped


def _refresh_pr_intel(output_dir: str | Path) -> None:
    """Best-effort: fresh blocked-PR intel for the briefing (wrap_up reads the cache)."""
    from auditor.pr_intel import pr_intel_enabled, refresh

    if not pr_intel_enabled():
        return
    try:
        refresh(output_dir=output_dir)
    except Exception as exc:  # the briefing falls back to the hourly cache
        logger.warning("pr intel refresh failed: %s", exc)
