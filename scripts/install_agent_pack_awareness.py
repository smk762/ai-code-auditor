#!/usr/bin/env python3
"""Install agent-pack awareness (AGENTS.md section + .cursor rule) into ecosystem repos.

Usage:
    python scripts/install_agent_pack_awareness.py [--dry-run] [--repo NAME] [--api-base URL] [--force]

Local files only — never runs git in the target repos.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.pack_awareness import DEFAULT_API_BASE, install_all  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing")
    ap.add_argument("--repo", default="", metavar="NAME", help="Install into a single ecosystem repo")
    ap.add_argument("--config", default="config/ecosystem.yaml", help="Ecosystem config path")
    ap.add_argument("--api-base", default="", metavar="URL",
                    help=f"Audit API base written into the files (default: literal {DEFAULT_API_BASE}, resolved by agents from their local .env)")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite an existing user-authored agent-coding-packs.mdc (no marker)")
    args = ap.parse_args(argv)

    try:
        results = install_all(
            ecosystem_config=args.config, repo=args.repo, api_base=args.api_base,
            dry_run=args.dry_run, force=args.force,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    changed = 0
    for r in results:
        if r.skipped:
            print(f"- {r.repo}: skipped ({r.skipped}: {r.path})")
            continue
        changed += r.changed
        print(f"- {r.repo} ({r.path})")
        for a in r.actions:
            print(f"    {a}")
    mode = "dry-run" if args.dry_run else "installed"
    print(f"\n{mode}: {len(results)} repo(s) considered, {changed} with changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
