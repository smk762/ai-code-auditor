"""Turn an "Ask this repo" answer that revealed a bug into an agent coding pack.

The pack then follows the normal loop (claim → repair → validate_patch), so a bug spotted in
chat reaches a PR the same way a scan finding does. The answer may itself be wrong, so the
pack says so and points at the code at the SHA the answer was given for.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from auditor.contracts import Finding
from wiki.citations import CITE

SOURCE = "ask"
MAX_TEXT = 6000


def _first_citation(answer_md: str) -> tuple[str, int]:
    m = CITE.search(answer_md or "")
    return (m.group(1), int(m.group(2))) if m else ("", 0)


def build_report_pack(repo: str, *, question: str, answer_md: str, note: str = "", source_sha: str = "",
                      repo_path: str | Path = "", public_base_url: str = ""):
    """An open pack for *repo* from an Ask exchange. Same exchange + note ⇒ same pack id."""
    from auditor.agent_pack import build_pack_from_finding

    note = (note or "").strip()
    question = (question or "").strip()
    if not note and not question:
        raise ValueError("a note or the question is required")
    file_path, line = _first_citation(answer_md)
    headline = (note or question).splitlines()[0][:140]
    at = f" at `{source_sha[:12]}`" if source_sha else ""
    description = "\n\n".join(p for p in [
        f"Reported from \"Ask this repo\"{at}.",
        f"**Reporter's note:** {note}" if note else "",
        f"**Question:** {question}" if question else "",
        f"**Answer that revealed it:**\n\n{(answer_md or '').strip()[:MAX_TEXT]}",
    ] if p)
    finding = Finding(
        id="", type="bug", severity="MEDIUM", repo=repo, file_path=file_path, line=line,
        title=headline, description=description, evidence=f"ask@{source_sha}" if source_sha else "ask",
        recommendation=("Confirm the problem against the code first: the answer came from wiki notes and "
                        "may be stale or wrong. If it is real, fix it; if not, set the pack to wontfix."),
        source=SOURCE,
    )
    pack = build_pack_from_finding(finding, repo_path=repo_path, kind="bug", run_id=source_sha,
                                   public_base_url=public_base_url)
    pack.provenance["question"] = question
    return pack


def report(output_dir: str | Path, repo: str, **kw: Any) -> dict[str, Any]:
    """Write the pack, refresh INDEX and its RAG doc. Returns {id, status, created}."""
    from auditor.agent_pack import (
        AgentCodingPack, ingest_packs_to_rag, load_pack, pack_rag_ingest_enabled, rebuild_index, write_pack,
    )

    base = kw.get("public_base_url", "")
    pack = build_report_pack(repo, **kw)
    existing = load_pack(output_dir, pack.work_item_id)
    write_pack(pack, output_dir)  # keeps the lifecycle of an existing pack with the same id
    rebuild_index(output_dir, public_base_url=base)
    data = load_pack(output_dir, pack.work_item_id) or pack.to_dict()
    if pack_rag_ingest_enabled():
        try:
            ingest_packs_to_rag([AgentCodingPack.from_dict(data)], output_dir=output_dir)
        except Exception:  # the pack is on disk either way; RAG catches up on the next emit
            pass
    return {"id": pack.work_item_id, "status": data.get("status", "open"), "created": existing is None}
