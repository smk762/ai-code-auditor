"""Install agent-pack awareness (AGENTS.md section + Cursor rule) into target repos.

Local files only: nothing here runs git. Installs are idempotent via marker comments:

* ``AGENTS.md`` — a marked section is appended (or replaced in place if present).
* ``.cursor/rules/agent-coding-packs.mdc`` — fully managed file (alwaysApply). An existing
  file *without* our marker is treated as user-authored and left alone unless ``force=True``.

CLI entry point: ``scripts/install_agent_pack_awareness.py`` (wrapped by the ``.sh`` script).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from auditor.config import load_ecosystem_config

DEFAULT_API_BASE = "http://192.168.1.198:8765"
MARKER_START = "<!-- ai-audit:agent-packs:start -->"
MARKER_END = "<!-- ai-audit:agent-packs:end -->"
RULE_RELPATH = Path(".cursor") / "rules" / "agent-coding-packs.mdc"
AGENTS_RELPATH = Path("AGENTS.md")


def _body(repo: str, api_base: str) -> str:
    base = api_base.rstrip("/")
    return f"""Do not wait for a pasted brief — fetch the audit packs for `{repo}` yourself.

1. **Bundle (one call):** `GET {base}/audit/packs/bundle?repo={repo}`
   (world model + ranked specialist packs + token-capped `briefs_markdown`;
   optional `&token_budget=2500&q=<keyword>&limit_specialists=5`).
   CLI equivalent: `ai-audit packs --bundle --repo {repo}`.
2. **Claim** the specialist pack you pick:
   `POST {base}/audit/packs/<id>/status` `{{"status":"claimed"}}`
   (send `X-API-Token: $AI_AUDIT_API_TOKEN`).
3. **Fix:** `ai-audit repair --pack-id <id>` (agent-composer repair loop; fetches the pack for you;
   HTTP: `POST http://127.0.0.1:9150/api/repair/run` `{{"pack_id":"<id>","mode":"review"}}`),
   or read `GET {base}/audit/packs/<id>` / `.../brief` and implement it yourself.
4. **Close:** `POST {base}/audit/validate_patch` `{{"repo":"{repo}","patch":"<diff>","pack_id":"<id>"}}`
   — a passing run marks the pack `resolved`.

World-model constraints (ownership, boundaries, scan branch `dev`) win over guesswork.
Full flow: ai-code-auditor `AGENTS.md`."""


def render_agents_section(repo: str, api_base: str = DEFAULT_API_BASE) -> str:
    return f"{MARKER_START}\n## Agent coding packs (ai-code-auditor)\n\n{_body(repo, api_base)}\n{MARKER_END}\n"


def render_rule(repo: str, api_base: str = DEFAULT_API_BASE) -> str:
    return (
        "---\n"
        f"description: Fetch ai-code-auditor coding packs for {repo} before changing code\n"
        "alwaysApply: true\n"
        "---\n\n"
        f"{MARKER_START}\n"
        "# Agent coding packs\n\n"
        f"{_body(repo, api_base)}\n"
        f"{MARKER_END}\n"
    )


@dataclass
class InstallResult:
    repo: str
    path: str
    actions: list[str] = field(default_factory=list)  # e.g. "AGENTS.md: created"
    skipped: str = ""  # reason the whole repo was skipped

    @property
    def changed(self) -> bool:
        return any(not a.endswith(": unchanged") and "skipped" not in a for a in self.actions)


def _splice_section(existing: str, section: str) -> str:
    """Replace the marked block in *existing* or append *section*."""
    start = existing.find(MARKER_START)
    end = existing.find(MARKER_END, start + 1) if start != -1 else -1
    if start != -1 and end != -1:
        tail = existing[end + len(MARKER_END):].lstrip("\n")
        head = existing[:start]
        return head + section + (("\n" + tail) if tail else "")
    if not existing:
        return section
    if existing.endswith("\n\n"):
        return existing + section
    return existing + ("\n" if existing.endswith("\n") else "\n\n") + section


def _write_if_changed(path: Path, new: str, existed: bool, old: str, *, dry_run: bool, label: str) -> str:
    if existed and old == new:
        return f"{label}: unchanged"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
    verb = "updated" if existed else "created"
    return f"{label}: {'would be ' if dry_run else ''}{verb}"


def install_into_repo(
    repo: str,
    repo_path: str | Path,
    *,
    api_base: str = DEFAULT_API_BASE,
    dry_run: bool = False,
    force: bool = False,
) -> InstallResult:
    root = Path(repo_path)
    result = InstallResult(repo=repo, path=str(root))
    if not root.is_dir():
        result.skipped = "path does not exist"
        return result

    # AGENTS.md — append / replace marked section.
    agents = root / AGENTS_RELPATH
    old = agents.read_text(encoding="utf-8") if agents.exists() else ""
    new = _splice_section(old, render_agents_section(repo, api_base))
    result.actions.append(
        _write_if_changed(agents, new, agents.exists(), old, dry_run=dry_run, label=str(AGENTS_RELPATH))
    )

    # .cursor/rules/agent-coding-packs.mdc — managed file.
    rule = root / RULE_RELPATH
    new_rule = render_rule(repo, api_base)
    if rule.exists():
        old_rule = rule.read_text(encoding="utf-8")
        if MARKER_START not in old_rule and not force:
            result.actions.append(f"{RULE_RELPATH}: skipped (user-authored, no marker; use --force)")
            return result
    else:
        old_rule = ""
    result.actions.append(
        _write_if_changed(rule, new_rule, rule.exists(), old_rule, dry_run=dry_run, label=str(RULE_RELPATH))
    )
    return result


def install_all(
    *,
    ecosystem_config: str | Path = "config/ecosystem.yaml",
    repo: str = "",
    api_base: str = "",
    dry_run: bool = False,
    force: bool = False,
) -> list[InstallResult]:
    """Install into enabled ecosystem repos that exist on disk (or just *repo*)."""
    base = (api_base or os.getenv("AI_AUDIT_PUBLIC_BASE_URL") or DEFAULT_API_BASE).rstrip("/")
    cfg = load_ecosystem_config(ecosystem_config)
    repos = cfg.repos
    if repo:
        repos = [r for r in repos if r.name == repo]
        if not repos:
            raise ValueError(f"repo {repo!r} not found among enabled repos in {ecosystem_config}")
    results: list[InstallResult] = []
    for r in repos:
        res = install_into_repo(r.name, r.path, api_base=base, dry_run=dry_run, force=force)
        results.append(res)
    return results
