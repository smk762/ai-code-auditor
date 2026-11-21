#!/usr/bin/env python3
"""Branch rules from CLAUDE.md, checked in CI (.github/workflows/branch-rules.yml) and by tests.

    pr-base --base <ref> --head <ref>     a PR into main must come from dev (the release PR)
    main-tree [--dev R] [--main R]        main must not be ahead of dev

"main not ahead of dev" is a tree rule, not a commit-graph one: a release either fast-forwards
main to a dev commit, or merges a reviewed PR from dev, which leaves one merge commit on main with
the same tree as the dev commit it merged. Both leave main's tree identical to the tree at the
merge base of dev and main, so ``git diff <merge-base> main`` must be empty. Comparing against the
dev tip instead would fail every time dev has unreleased work. Anything else committed to main
(a hotfix pushed there, a merge of a feature branch) changes that diff and fails.

Exit 0 when the rule holds, 1 when it is broken, 2 on a usage or git error.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

RELEASE_BASE = "main"
RELEASE_HEAD = "dev"


def check_pr_base(base: str, head: str) -> str | None:
    """The problem with a PR from ``head`` into ``base``, or None when it is allowed."""
    if base == RELEASE_BASE and head != RELEASE_HEAD:
        return (f"PR from {head!r} into {RELEASE_BASE!r}: only the release PR from {RELEASE_HEAD!r} may "
                f"target {RELEASE_BASE!r}. Retarget it to {RELEASE_HEAD!r}.")
    return None


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)


def _resolves(repo: str, ref: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def check_main_tree(repo: str, dev: str, main: str) -> str | None:
    """The problem with ``main`` relative to ``dev``, or None when main is not ahead of dev."""
    for ref in (dev, main):
        if ref.startswith("-") or not _resolves(repo, ref):
            raise ValueError(f"cannot resolve {ref!r} in {repo}")
    base = _git(repo, "merge-base", dev, main)
    if base.returncode != 0:
        return f"{main} and {dev} share no history"
    base_sha = base.stdout.strip()
    diff = _git(repo, "diff", "--stat", base_sha, main, "--")
    if diff.returncode != 0:
        raise ValueError(diff.stderr.strip())
    if not diff.stdout.strip():
        return None
    extra = _git(repo, "log", "--oneline", "--no-decorate", f"{dev}..{main}").stdout.strip()
    return (f"{main} is ahead of {dev}: its tree differs from their merge base {base_sha[:12]}.\n"
            f"{diff.stdout.rstrip()}\nCommits on {main} that {dev} lacks:\n{extra or '(none)'}\n"
            f"Land the change on {dev} and release it; never commit to {main} directly.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("pr-base", help="a PR into main must come from dev")
    pr.add_argument("--base", required=True)
    pr.add_argument("--head", required=True)
    tree = sub.add_parser("main-tree", help="main must not be ahead of dev")
    tree.add_argument("--repo", default=".")
    tree.add_argument("--dev", default="origin/dev")
    tree.add_argument("--main", default="origin/main")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "pr-base":
            problem = check_pr_base(args.base, args.head)
            ok = f"PR {args.head} -> {args.base} is allowed"
        else:
            problem = check_main_tree(args.repo, args.dev, args.main)
            ok = f"{args.main} is not ahead of {args.dev}"
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if problem:
        print(problem, file=sys.stderr)
        return 1
    print(ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
