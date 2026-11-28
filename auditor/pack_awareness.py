"""Install agent-pack awareness (AGENTS.md section + Cursor rule + guard hook) into working checkouts.

Targets are where people work: ``~/<name>`` for each scanned repo that is checked out there, or the
paths given. Never a scan mirror: anything resolving into ``mirror_root`` or onto a configured repo
or mirror path is skipped, since scans and validation only ever read those. ``hook_only`` installs
just the guard hook and leaves the tracked files alone.

Local files only: nothing here runs git. Installs are idempotent via marker comments:

* ``AGENTS.md`` — a marked section is appended (or replaced in place if present).
* ``.cursor/rules/agent-coding-packs.mdc`` — fully managed file (alwaysApply). An existing
  file *without* our marker is treated as user-authored and left alone unless ``force=True``.
* ``.github/pull_request_template.md`` — a marked "Audit packs" section (#73), spliced like AGENTS.md.
* ``.github/ISSUE_TEMPLATE/audit-finding.yml`` — managed issue form (#73), same rule as the Cursor rule.
* ``.claude/settings.local.json`` — the write-time guard hook (``auditor.guard_hook``): a Claude Code
  ``PostToolUse`` entry merged into the file, never replacing the user's settings. The command holds
  this host's interpreter path, so the file must stay out of git: the install adds it to
  ``.git/info/exclude`` unless an ignore file already names it, and refuses the hook when it can't.

CLI entry point: ``scripts/install_agent_pack_awareness.py`` (wrapped by the ``.sh`` script).
"""
from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from auditor.config import load_ecosystem_config

# Committed files must not carry a real LAN address: agents read the base URL from the env
# var at runtime (real value lives in each machine's local .env).
DEFAULT_API_BASE = "$AI_AUDIT_PUBLIC_BASE_URL"
MARKER_START = "<!-- ai-audit:agent-packs:start -->"
MARKER_END = "<!-- ai-audit:agent-packs:end -->"
RULE_RELPATH = Path(".cursor") / "rules" / "agent-coding-packs.mdc"
AGENTS_RELPATH = Path("AGENTS.md")
PR_TEMPLATE_RELPATH = Path(".github") / "pull_request_template.md"
ISSUE_FORM_RELPATH = Path(".github") / "ISSUE_TEMPLATE" / "audit-finding.yml"
PR_MARKER_START = "<!-- ai-audit:pr-template:start -->"
PR_MARKER_END = "<!-- ai-audit:pr-template:end -->"
FORM_MARKER = "# ai-audit:agent-packs managed file"
CLAUDE_SETTINGS_RELPATH = Path(".claude") / "settings.local.json"
HOOK_MODULE = "auditor.guard_hook"  # how our hook entry is recognised in a settings file
HOOK_MATCHER = "Edit|Write|MultiEdit"
HOOK_TIMEOUT_S = 60


def _body(repo: str, api_base: str) -> str:
    base = api_base.rstrip("/")
    return f"""Do not wait for a pasted brief — fetch the audit packs for `{repo}` yourself.

The audit API base URL is `$AI_AUDIT_PUBLIC_BASE_URL`. The real host/IP is in the local `.env`
(see `.env.example`, placeholder `http://<AUDIT_HOST>:8765`); read it there, don't guess.

1. **Bundle (one call):** `GET {base}/audit/packs/bundle?repo={repo}`
   (world model + ranked specialist packs + token-capped `briefs_markdown`;
   optional `&token_budget=2500&q=<keyword>&limit_specialists=5`).
   CLI equivalent: `ai-audit packs --bundle --repo {repo}`.
2. **Claim** the specialist pack you pick:
   `POST {base}/audit/packs/<id>/status` `{{"status":"claimed"}}`
   (send `X-API-Token: $AI_AUDIT_API_TOKEN`; the token is in your environment, set for Claude Code
   in `~/.claude/settings.json`. If it is empty or you get a 401, ask the user; don't search env files).
3. **Fix:** `ai-audit repair --pack-id <id>` (agent-composer repair loop; fetches the pack for you;
   HTTP: `POST http://127.0.0.1:9150/api/repair/run` `{{"pack_id":"<id>","mode":"review"}}`),
   or read `GET {base}/audit/packs/<id>` / `.../brief` and implement it yourself.
4. **Close:** `POST {base}/audit/validate_patch` `{{"repo":"{repo}","patch":"<diff>","pack_id":"<id>"}}`
   — a pass resolves the pack only with evidence: for a bug or security pack, include a test that
   **fails on `dev` and passes with your fix** (validate_patch runs it on both trees). Otherwise
   the pack becomes `unverified` and the response's `pack.reason` says what's missing. If no test
   can exercise it, add `"evidence_waiver":"<why>"` (recorded and counted).
5. **Commit:** end each fix commit with trailers, one `Pack: <id>` per pack it fixes, plus
   `Evidence: <kind>` (from validate_patch) and `Class: <class>` when the pack has one.
   `GET {base}/audit/pr-summary?packs=<id>,<id>&evidence=<kind>` returns the exact trailer block and
   the table for the PR's "Audit packs" section. A revert then maps to exactly those packs.

**Guard tasks:** a `pack-class-<class>` pack eliminates a whole class of finding with one guard
(a lint rule or a shared helper). When the bundle offers one, prefer it over fixing its children
one by one; its brief says how to record the guard.

**Search** before crawling: `GET {base}/audit/search?q=<question>&repo={repo}&kind=wiki`
(wiki sections and packs with links; CLI `ai-audit search "<question>" --repo {repo}`).

World-model constraints (ownership, boundaries, scan branch `dev`) win over guesswork.
Full flow: ai-code-auditor `AGENTS.md`."""


def render_agents_section(repo: str, api_base: str = DEFAULT_API_BASE) -> str:
    return f"{MARKER_START}\n## Agent coding packs (ai-code-auditor)\n\n{_body(repo, api_base)}\n{MARKER_END}\n"


def render_pr_section() -> str:
    return (
        f"{PR_MARKER_START}\n"
        "### Audit packs\n\n"
        "<!-- Fixing ai-code-auditor packs? End each commit with `Pack: <id>` trailers (plus `Evidence:` and "
        "`Class:`), then replace this comment with the table from "
        "GET $AI_AUDIT_PUBLIC_BASE_URL/audit/pr-summary?packs=<id>,<id>. Not fixing packs? Delete this section. -->\n"
        f"{PR_MARKER_END}\n"
    )


def _managed_span(existing: str, start_marker: str, end_marker: str) -> tuple[str, int, int]:
    """``(text, start, end)`` of the managed block, or ``(text, -1, -1)`` to append one.

    The block starts at the start marker nearest before the first end marker, so a stale start
    marker above it can't widen the replaced range over user text. A start marker with no end
    marker after it is dropped (only the marker, never the text after it), so the appended block
    is the only one and the next splice can't reach back to the orphan."""
    start = existing.find(start_marker)
    end = existing.find(end_marker, start + 1) if start != -1 else -1
    if start != -1 and end != -1:
        return existing, existing.rfind(start_marker, 0, end), end
    if start != -1:
        existing = existing.replace(start_marker + "\n", "").replace(start_marker, "")
    return existing, -1, -1


def _splice_pr(existing: str, section: str) -> str:
    existing, start, end = _managed_span(existing, PR_MARKER_START, PR_MARKER_END)
    if start != -1 and end != -1:
        tail = existing[end + len(PR_MARKER_END):].lstrip("\n")
        return existing[:start] + section + (("\n" + tail) if tail else "")
    if not existing:
        return section
    return existing.rstrip("\n") + "\n\n" + section


def render_issue_form() -> str:
    """A GitHub issue form whose answers parse by heading (``trailers.issue_fields``)."""
    import json

    from auditor.finding_class import taxonomy

    classes = [*taxonomy().ids, "logic_error", "other / unsure"]
    q = json.dumps  # YAML accepts JSON strings, so nothing needs hand-escaping
    return "\n".join([
        f"{FORM_MARKER} (ai-code-auditor scripts/install_agent_pack_awareness.sh); edits are overwritten.",
        "name: Audit finding",
        "description: A code finding for the ai-code-auditor pack flow, one per issue",
        'title: "[finding] "',
        "body:",
        "  - type: input",
        "    id: pack",
        "    attributes:",
        "      label: Pack id",
        f"      description: {q('The pack-<repo>-<hash> id from /ui/packs, if the auditor already has one')}",
        "      placeholder: pack-",
        "  - type: input",
        "    id: location",
        "    attributes:",
        "      label: Location",
        "      placeholder: path/to/file.py:123",
        "    validations:",
        "      required: true",
        "  - type: dropdown",
        "    id: class",
        "    attributes:",
        "      label: Finding class",
        f"      description: {q('The class of mistake (ai-code-auditor config/finding_classes.yaml)')}",
        f"      options: [{', '.join(q(c) for c in classes)}]",
        "  - type: input",
        "    id: evidence",
        "    attributes:",
        "      label: Evidence",
        f"      description: {q('A link to a failing test, a log or the pack page')}",
        "  - type: textarea",
        "    id: what",
        "    attributes:",
        "      label: What is wrong",
        "    validations:",
        "      required: true",
        "",
    ])


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
    existing, start, end = _managed_span(existing, MARKER_START, MARKER_END)
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


def hook_command(python: str | None = None) -> str:
    """The hook's command line: this interpreter, isolated (``-I``) so the target repo's own cwd and
    ``PYTHON*`` variables can't shadow the ``auditor`` package."""
    return f"{shlex.quote(python or sys.executable)} -I -m {HOOK_MODULE}"


def _is_ours(entry: object) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(h, dict) and HOOK_MODULE in str(h.get("command") or "") for h in entry.get("hooks") or [])


def merge_guard_hook(settings: dict, command: str) -> dict:
    """*settings* with exactly one guard-hook entry under ``hooks.PostToolUse`` (ours replaced, every
    other entry and key kept as it was)."""
    out = dict(settings)
    hooks = dict(out.get("hooks") or {})
    post = [e for e in hooks.get("PostToolUse") or [] if not _is_ours(e)]
    post.append({"matcher": HOOK_MATCHER,
                 "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]})
    hooks["PostToolUse"] = post
    out["hooks"] = hooks
    return out


def _ignored(root: Path, rel: str) -> bool | None:
    """Whether an ignore file names *rel* (textually; nothing here runs git). None when the repo has
    no ``.git`` directory to hold a local exclude (a worktree or submodule's ``.git`` is a file)."""
    for f in (root / ".gitignore", root / ".claude" / ".gitignore", root / ".git" / "info" / "exclude"):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        names = {ln.strip().lstrip("/") for ln in text.splitlines()}
        if rel in names or Path(rel).name in names or ".claude/" in names or ".claude" in names:
            return True
    return False if (root / ".git").is_dir() else None


def _install_guard_hook(root: Path, *, python: str | None, dry_run: bool) -> list[str]:
    label = str(CLAUDE_SETTINGS_RELPATH)
    rel = CLAUDE_SETTINGS_RELPATH.as_posix()
    ignored = _ignored(root, rel)
    if ignored is None:
        return [f"{label}: skipped (no .git directory to exclude it from; add {rel} to .gitignore first)"]
    actions = []
    if not ignored:
        exclude = root / ".git" / "info" / "exclude"
        if not dry_run:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            old_ex = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            exclude.write_text(old_ex + ("" if not old_ex or old_ex.endswith("\n") else "\n") + f"/{rel}\n",
                               encoding="utf-8")
        actions.append(f".git/info/exclude: {'would add' if dry_run else 'added'} /{rel}")
    path = root / CLAUDE_SETTINGS_RELPATH
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        current = json.loads(old) if old.strip() else {}
    except ValueError:
        return [*actions, f"{label}: skipped (not valid JSON; fix it and re-run)"]
    if not isinstance(current, dict):
        return [*actions, f"{label}: skipped (not a JSON object)"]
    new = json.dumps(merge_guard_hook(current, hook_command(python)), indent=2) + "\n"
    if old.strip() and json.loads(old) == json.loads(new):
        return [*actions, f"{label}: unchanged"]
    return [*actions, _write_if_changed(path, new, path.exists(), old, dry_run=dry_run, label=label)]


def install_into_repo(
    repo: str,
    repo_path: str | Path,
    *,
    api_base: str = DEFAULT_API_BASE,
    dry_run: bool = False,
    force: bool = False,
    guard_hook: bool = True,
    python: str | None = None,
    hook_only: bool = False,
) -> InstallResult:
    root = Path(repo_path)
    result = InstallResult(repo=repo, path=str(root))
    if not root.is_dir():
        result.skipped = "path does not exist"
        return result
    if hook_only:
        result.actions.extend(_install_guard_hook(root, python=python, dry_run=dry_run))
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
        else:
            result.actions.append(
                _write_if_changed(rule, new_rule, True, old_rule, dry_run=dry_run, label=str(RULE_RELPATH)))
    else:
        result.actions.append(_write_if_changed(rule, new_rule, False, "", dry_run=dry_run, label=str(RULE_RELPATH)))

    # .github/pull_request_template.md — marked section (#73).
    tpl = root / PR_TEMPLATE_RELPATH
    old_tpl = tpl.read_text(encoding="utf-8") if tpl.exists() else ""
    result.actions.append(_write_if_changed(tpl, _splice_pr(old_tpl, render_pr_section()), tpl.exists(), old_tpl,
                                            dry_run=dry_run, label=str(PR_TEMPLATE_RELPATH)))

    # .github/ISSUE_TEMPLATE/audit-finding.yml — managed file (#73).
    form = root / ISSUE_FORM_RELPATH
    old_form = form.read_text(encoding="utf-8") if form.exists() else ""
    if form.exists() and FORM_MARKER not in old_form and not force:
        result.actions.append(f"{ISSUE_FORM_RELPATH}: skipped (user-authored, no marker; use --force)")
    else:
        result.actions.append(_write_if_changed(form, render_issue_form(), form.exists(), old_form,
                                                dry_run=dry_run, label=str(ISSUE_FORM_RELPATH)))

    # .claude/settings.local.json — the write-time guard hook, merged into local, untracked settings.
    if guard_hook:
        result.actions.extend(_install_guard_hook(root, python=python, dry_run=dry_run))
    return result


def scan_sources(ecosystem_config: str | Path = "config/ecosystem.yaml",
                 mirrors_config: str | Path = "config/repo_mirrors.yaml") -> tuple[set[Path], list[Path]]:
    """Resolved scan-source directories (every configured repo path and mirror) and the mirror roots.
    The installer never writes into one: scans and validation only read them (CLAUDE.md)."""
    from conductor.mirrors import load_mirrors_config

    mcfg = load_mirrors_config(mirrors_config)
    dirs = {Path(str(m["path"])).resolve() for m in mcfg.get("mirrors") or [] if m.get("path")}
    dirs |= {Path(r.path).resolve() for r in load_ecosystem_config(ecosystem_config).repos if r.path}
    roots = [Path(str(mcfg["mirror_root"])).resolve()] if mcfg.get("mirror_root") else []
    return dirs, roots


def _scan_source(path: Path, dirs: set[Path], roots: list[Path]) -> bool:
    p = path.resolve()  # through symlinks: ~/argus-core is the cortex mirror
    return p in dirs or any(p == r or p.is_relative_to(r) for r in roots)


def install_all(
    *,
    ecosystem_config: str | Path = "config/ecosystem.yaml",
    mirrors_config: str | Path = "config/repo_mirrors.yaml",
    repo: str = "",
    paths: list[str | Path] | None = None,
    home: str | Path | None = None,
    api_base: str = "",
    dry_run: bool = False,
    force: bool = False,
    guard_hook: bool = True,
    hook_only: bool = False,
) -> list[InstallResult]:
    """Install into the working checkouts of the scanned repos: ``<home>/<name>`` for each enabled
    repo (or just *repo*), or the given *paths* instead. Never into a scan source: every configured
    repo path is a mirror (``test_repo_sets``), and anything that resolves into one is skipped."""
    from auditor.fsutil import host_path

    # Deliberately not os.getenv("AI_AUDIT_PUBLIC_BASE_URL"): that would bake the real LAN
    # address into public repos. Pass --api-base explicitly to override.
    base = (api_base or DEFAULT_API_BASE).rstrip("/")
    cfg = load_ecosystem_config(ecosystem_config)
    dirs, roots = scan_sources(ecosystem_config, mirrors_config)
    if paths:
        targets = [(Path(p).expanduser().name, Path(p).expanduser()) for p in paths]
    else:
        repos = cfg.repos
        if repo:
            repos = [r for r in repos if r.name == repo]
            if not repos:
                raise ValueError(f"repo {repo!r} not found among enabled repos in {ecosystem_config}")
        root = Path(home) if home is not None else Path(host_path("~"))
        targets = [(r.name, root / r.name) for r in repos]
    results: list[InstallResult] = []
    for name, path in targets:
        if _scan_source(path, dirs, roots):
            results.append(InstallResult(repo=name, path=str(path), skipped="a scan mirror, never written"))
            continue
        if path.is_dir() and not (path / ".git").exists():
            results.append(InstallResult(repo=name, path=str(path), skipped="not a git checkout"))
            continue
        results.append(install_into_repo(name, path, api_base=base, dry_run=dry_run, force=force,
                                         guard_hook=guard_hook or hook_only, hook_only=hook_only))
    return results
