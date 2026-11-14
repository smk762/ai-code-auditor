""""Ask this repo": a question answered from one repo's wiki, notes and structure facts.

The context is what the wiki was built from (file notes with line ranges, AST structure
facts, README, ecosystem doc) plus the repo's current wiki pages, so answers can cite exact
lines. It's identical for every question about the same repo, so with Claude it is prompt
cached and follow-up questions cost a fraction of the first. The model is the auditor's
configured one (``AI_AUDIT_LLM_PROVIDER`` / ``AI_AUDIT_LLM_MODEL``, per #8: "existing copilot
model"). Answers go through the same citation pass as wiki pages: claims whose citation fails
are dropped, and the rest link to GitHub. Spend goes to the ``ask`` lane of the ledger.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from wiki import citations
from wiki import notes as notes_mod
from wiki import pages as pages_mod
from wiki.sources import structure_block

LANE = "ask"
MAX_QUESTION_CHARS = 2000
MAX_TOKENS = 4000

ASK_SYSTEM = """You answer questions about one repository of the Argus ecosystem for engineers \
and coding agents. Below are the repository's wiki pages, per-file notes (with line ranges), \
structure facts read from the code, and the shared ecosystem doc.

Rules:
- Answer from this context only. If it doesn't contain the answer, say so plainly and suggest \
where in the repository to look.
- Cite every factual claim about the code as [path:Lstart-Lend], using repo-relative paths and \
line numbers from the notes or the wiki's own citations. Never invent a path or line number.
- Be concise: lead with the direct answer, then the supporting detail. GitHub-flavoured markdown, \
no level-1 heading."""


def _context(repo, output_dir: Path) -> str:
    notes = notes_mod.load_notes(repo, output_dir)
    ctx = pages_mod.repo_context(repo, notes, structure_block(repo))
    d = output_dir / "wiki" / repo.name
    wiki = []
    for slug in pages_mod.PAGES:
        p = d / f"{slug}.md"
        if p.is_file():
            wiki.append(p.read_text(encoding="utf-8"))
    if wiki:
        ctx = "## Wiki pages (current)\n\n" + "\n\n---\n\n".join(wiki) + "\n\n" + ctx
    return ctx


def _anthropic(model: str, effort: str, context: str, question: str, client: Any = None) -> tuple[str, str, float]:
    from auditor.spend_ledger import get_ledger

    ledger = get_ledger()
    ledger.check(LANE)
    if client is None:
        import anthropic

        client = anthropic.Anthropic(max_retries=2)
    msg = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        output_config={"effort": effort},
        system=[{"type": "text", "text": ASK_SYSTEM},
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}],
    )
    cost = ledger.record_usage(LANE, getattr(msg, "model", model) or model, msg.usage, note="ask")
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return text, msg.stop_reason or "", cost


def ask(output_dir: str | Path, repo_name: str, question: str, *, client: Any = None) -> dict[str, Any]:
    from auditor.config import load_ecosystem_config
    from auditor.config import load_model_provider_config
    from auditor.repo_resolver import resolve_scan_root

    question = (question or "").strip()
    if not question:
        raise ValueError("empty question")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question longer than {MAX_QUESTION_CHARS} characters")
    out = Path(output_dir)
    repo = next((r for r in load_ecosystem_config().repos if r.enabled and r.name == repo_name), None)
    if repo is None:
        raise LookupError(f"unknown repo {repo_name!r}")
    context = _context(repo, out)
    cfg = load_model_provider_config()
    effort = os.getenv("AI_AUDIT_ASK_EFFORT", "low")
    if cfg.provider == "anthropic":
        text, stop, cost = _anthropic(cfg.model, effort, context, question, client)
    else:  # other providers: the audit client's plain generate (no prompt caching)
        from auditor.llm_client import LLMClient

        # the lane goes to this client only: setting it in os.environ would leak into the API
        # process and every audit subprocess it starts
        text, stop, cost = LLMClient(lane=LANE).generate(f"{ASK_SYSTEM}\n\n{context}\n\n## Question\n{question}"), "", 0.0
    if stop == "refusal" or not text.strip():
        return {"repo": repo_name, "question": question, "answer_md": "", "model": cfg.model, "cost_usd": cost,
                "error": "The model declined or returned nothing; try rephrasing the question."}
    root = resolve_scan_root(repo)
    md, dropped = citations.drop_failed_claims(text, root)
    return {"repo": repo_name, "question": question, "answer_md": md, "dropped_claims": dropped,
            "citations": citations.check(md, root).to_dict(), "model": cfg.model, "cost_usd": round(cost, 5),
            "truncated": stop == "max_tokens"}
