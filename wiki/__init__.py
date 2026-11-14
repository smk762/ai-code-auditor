"""DeepWiki-style generated wiki per repo (ai-code-auditor#8, section 1).

Pipeline (models per the #8 decisions, all through the Message Batches API):
per-file notes (Haiku, cached by content hash) → page drafts (Opus) from notes, README, the
ecosystem doc and AST structure facts → check-and-fix (Opus) against the exact cited code →
mechanical citation check that drops claims whose citation fails → reports/wiki/<repo>/.
The "Notable findings" page is built from the findings DB and packs, without an LLM.
"""
