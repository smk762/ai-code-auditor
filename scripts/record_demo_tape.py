#!/usr/bin/env python3
"""Record the hosted demo's tape (#199) from demo/run.yaml: python scripts/record_demo_tape.py [--out PATH]

Reads the run's packs from reports/agent_packs/ and each fix commit's diff from the repo's mirror (``git show``,
read only), builds the tape (auditor/demo_tape.py) and writes demo/tape/tape.json only if every string on it
passes the public wiki's opsec gate, with the deny-list (``AI_AUDIT_OPSEC_DENYLIST``) and the private repo names
GitHub reports. Run it on the host where the packs live; commit the tape it writes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auditor import demo_tape
from auditor.agent_pack import packs_dir
from auditor.config import load_ecosystem_config
from auditor.merge_poll import github_token
from wiki.public import OWNER, github_visibility, load_denylist, private_repo_names

RUN = ROOT / "demo" / "run.yaml"
TAPE = ROOT / "demo" / "tape" / "tape.json"


def mirror_of(repo: str) -> Path:
    for r in load_ecosystem_config().repos:
        if r.name == repo:
            return Path(r.path)
    raise SystemExit(f"{repo} is not a configured repo")


def git(mirror: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(mirror), *args], check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=TAPE)
    ap.add_argument("--reports", type=Path, default=None, help="reports dir holding agent_packs/ (default: config)")
    args = ap.parse_args()
    pack_dir = packs_dir(args.reports or load_ecosystem_config().output_dir)

    run = yaml.safe_load(RUN.read_text(encoding="utf-8"))
    repo = run["repo"]
    if github_visibility(repo, github_token()) != "public":
        raise SystemExit(f"{repo} isn't reported public by GitHub; a public demo can't replay it")
    mirror = mirror_of(repo)
    repo_url = f"https://github.com/{OWNER}/{repo}"

    packs = [yaml.safe_load((pack_dir / f"{pid}.yaml").read_text(encoding="utf-8"))
             for pid in run["packs"] if (pack_dir / f"{pid}.yaml").is_file()]
    commits = {}
    for short in {str(s) for s in run["packs"].values() if s}:
        sha = git(mirror, "rev-parse", "--verify", f"{short}^{{commit}}").strip()
        subject = git(mirror, "show", "-s", "--format=%s", sha).strip()
        diff = git(mirror, "show", "--format=", "--no-color", "--no-ext-diff", sha)
        commits[short] = demo_tape.commit_entry(sha, subject, diff, repo_url)

    tape = demo_tape.build_tape(run, packs, commits, repo_url=repo_url,
                                recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    token = github_token()
    vis = {r.name: github_visibility(r.name, token) for r in load_ecosystem_config().repos}
    demo_tape.check_public(tape, load_denylist(), private_repo_names(vis))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(tape, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    c = tape["counts"]
    print(f"wrote {args.out.relative_to(ROOT)}: {c['findings']} packs, {c['resolved']} resolved "
          f"({c['repro']} repro, {c['test_only']} test_only, {c['waiver']} waiver), {c['wontfix']} wontfix, "
          f"{c['commits']} commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
