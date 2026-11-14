"""Page prompts and request builders: draft (from notes + structure facts) and check-and-fix.

The setup is the one the 2026-11-13 eval chose ("opus-v2v", see #8): Opus at medium effort
drafts each page from the file notes, README, ecosystem doc and AST structure facts, then a
second Opus pass checks every claim against the exact code its citations point at.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from auditor.repo_resolver import resolve_repo_path

PAGE_MODEL = "claude-opus-5-5"
PAGE_EFFORT = "medium"
PAGE_MAX_TOKENS = 32000
ECOSYSTEM_DOC = Path(__file__).resolve().parents[1] / "ecosystem" / "ecosystem.md"
README_MAX_CHARS = 20_000

# slug → (title, brief). Order is the wiki's page order; "findings" is added from data.
PAGES: dict[str, tuple[str, str]] = {
    "overview": ("Overview",
                 "Overview: what this repository is for, who/what uses it, its main capabilities, "
                 "and how it fits into the wider Argus ecosystem. Orientation for a newcomer."),
    "architecture": ("Architecture",
                     "Architecture: the main modules/packages, their responsibilities, and how data "
                     "and control flow between them. Include a mermaid diagram if it genuinely helps."),
    "interfaces": ("Interfaces and entry points",
                   "Interfaces and entry points: HTTP routes, CLI commands, workers/jobs, public "
                   "functions or classes other code calls, config and env vars that change behaviour."),
    "integration": ("Integration",
                    "Integration: what this repo depends on and what depends on it — other ecosystem "
                    "repos, external services, databases, queues, model providers — and the contracts "
                    "(payloads, schemas, endpoints) at each boundary."),
}

PAGE_SYSTEM = """You write pages for a DeepWiki-style developer wiki of one repository in the \
Argus ecosystem. Readers are engineers and coding agents new to the repo.

Rules:
- Every factual claim about the code must cite its source as [path:Lstart-Lend] using the \
repo-relative paths and line numbers from the file notes. Cite the narrowest range that supports \
the claim. Never invent a path or line number.
- Only state what the notes and context support. If something is unclear, say so briefly rather \
than guessing.
- Write GitHub-flavoured markdown. Start with a level-1 heading. Prefer short sections, tables \
for lists of routes/commands/env vars, and concrete names over generic prose.
- Length: as long as the content warrants, typically 400-1200 words."""

VERIFY_SYSTEM = """You are fact-checking a draft wiki page against the repository's actual code. \
You are given the structure facts (imports/exports read from the code), the exact code for \
every range the draft cites (line-numbered), and the draft.

For every claim in the draft:
- If the cited code supports it, keep it.
- If the cited code contradicts it, or the claim over-generalises ("all", "every", \
"everything", "only"), rewrite it to match the code exactly, or delete it.
- If the citation points at the wrong lines, fix the line numbers using the code shown; if \
you cannot find supporting lines in what is shown, remove the claim rather than guess.
- Claims about imports, re-exports, dependency direction and optional dependencies must match \
the structure facts.
Do not add new sections or new claims. Keep the draft's structure, headings and tone. \
Output only the corrected page markdown, nothing else."""


def repo_context(repo, notes: list[dict[str, Any]], structure: str) -> str:
    """The shared, cacheable context every page of one repo is drafted from."""
    parts = [f"# Repository: {repo.name}\n"]
    if ECOSYSTEM_DOC.is_file():
        parts += ["## Ecosystem topology (shared doc)\n", ECOSYSTEM_DOC.read_text(encoding="utf-8")]
    try:
        base = resolve_repo_path(repo)
        readme = next(iter(sorted(base.glob("README*"))), None)
    except Exception:  # noqa: BLE001 — a missing README is fine
        readme = None
    if readme:
        parts += [f"\n## {readme.name}\n", readme.read_text(encoding="utf-8", errors="replace")[:README_MAX_CHARS]]
    parts.append(f"\n## File notes ({len(notes)} files)\n")
    for n in sorted(notes, key=lambda n: n["path"]):
        parts.append(f"\n### {n['path']} ({n['lines']} lines) — {n['role']}\n{n['summary']}")
        for s in n["key_symbols"]:
            parts.append(f"- {s['kind']} `{s['name']}` L{s['start_line']}-L{s['end_line']}: {s['purpose']}")
        if n["interfaces"]:
            parts.append("  interfaces: " + "; ".join(n["interfaces"]))
        if n["dependencies"]:
            parts.append("  deps: " + "; ".join(n["dependencies"]))
    parts.append("\n\n" + structure)
    return "\n".join(parts)


def draft_params(page: str, context: str) -> dict[str, Any]:
    _, brief = PAGES[page]
    return {
        "model": PAGE_MODEL,
        "max_tokens": PAGE_MAX_TOKENS,
        "output_config": {"effort": PAGE_EFFORT},
        # the repo context is identical for every page of a repo, so it is cached
        "system": [{"type": "text", "text": PAGE_SYSTEM},
                   {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": f"Write the **{page}** page.\n\nBrief: {brief}"}],
    }


def verify_params(page: str, draft: str, structure: str, cited: str) -> dict[str, Any]:
    _, brief = PAGES[page]
    return {
        "model": PAGE_MODEL,
        "max_tokens": PAGE_MAX_TOKENS,
        "output_config": {"effort": PAGE_EFFORT},
        "system": [{"type": "text", "text": VERIFY_SYSTEM},
                   {"type": "text", "text": structure, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": f"## Cited code\n{cited}\n\n## Page brief\n{brief}\n\n## Draft\n{draft}"}],
    }
