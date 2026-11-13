"""TTS-safe LATEST.md writer for Holly / mithrandir companion."""

from __future__ import annotations

import re
from typing import Sequence

from conductor.publish import count_words, md_passes_tts_rules
from conductor.schemas import DecisionNeeded, DraftPr, MithrandirStatus


def _pr_spoken(pr: DraftPr) -> str:
    """Speak as repo#N when possible."""
    m = re.search(r"/pull/(\d+)|/merge_requests/(\d+)|#(\d+)$", pr.url or "")
    num = None
    if m:
        num = next(g for g in m.groups() if g)
    if num:
        return f"{pr.repo}#{num}"
    return pr.repo or "a pull request"


def _outcome_phrase(reason: str) -> str:
    if reason == "completed":
        return "The rest session completed normally."
    if reason == "time_budget":
        return "The rest session wrapped up for the time budget."
    if reason == "escalation":
        return "The rest session paused for a human decision."
    return "The rest session finished."


def _fit_pr_brief(brief: str, *, used: int, limit: int = 150) -> list[str]:
    """Blocked-PR sentences that still fit under the spoken word limit, whole sentences only."""
    out: list[str] = []
    for sentence in re.split(r"(?<=\.)\s+", (brief or "").strip()):
        n = count_words(sentence)
        if not sentence or used + n > limit:
            break
        out.append(sentence)
        used += n
    return out


def render_speakable_markdown(status: MithrandirStatus) -> str:
    """Produce ~150-word prose suitable for TTS. May set speakable false upstream if invalid."""
    confirmed = status.findings.confirmed
    pr_count = len(status.draft_prs)
    parts: list[str] = [_outcome_phrase(status.ended_reason)]
    parts.append(
        f"There were {confirmed} confirmed finding{'s' if confirmed != 1 else ''} "
        f"and {pr_count} draft pull request{'s' if pr_count != 1 else ''}."
    )

    if status.draft_prs:
        named = []
        for pr in status.draft_prs[:2]:
            named.append(f"{_pr_spoken(pr)} marked {pr.action.replace('_', ' ')}")
        parts.append("Drafts include " + "; ".join(named) + ".")

    decisions: Sequence[DecisionNeeded] = status.decisions_needed[:2]
    if decisions:
        bits = [d.summary.rstrip(".") for d in decisions]
        parts.append("Decisions needed: " + "; ".join(bits) + ".")
    elif status.findings.top:
        top = status.findings.top[0]
        parts.append(
            f"Top item: {top.severity.lower()} in {top.repo} — {top.title.rstrip('.')}."
        )
    else:
        parts.append("No blocking decisions tonight.")

    tail = [f"Next rest window starts {status.next_rest_window}."] if status.next_rest_window else []
    parts.extend(_fit_pr_brief(status.pr_brief, used=count_words(" ".join(parts + tail))))
    parts.extend(tail)

    text = " ".join(parts)
    # Trim to ~150 words if needed
    words = text.split()
    if len(words) > 150:
        text = " ".join(words[:150])
    return text


def build_briefing(status: MithrandirStatus) -> tuple[str, bool]:
    """Return (body_md, speakable). body_md is also written to LATEST.md."""
    md = render_speakable_markdown(status)
    ok, _ = md_passes_tts_rules(md)
    if count_words(md) == 0:
        return "The rest session finished with no speakable summary.", False
    return md, ok
