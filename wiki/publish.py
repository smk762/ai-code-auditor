"""Publish the gated wiki bundle to the private docs repo (smk762/argus-docs).

dragonhound-site deploys by direct upload from a local build, and pins a docs commit in its
``docs.lock.json`` (repo, commit, bundle_sha256); bumping that pin is the review gate before
docs go live. So this step ends at a commit (and, with ``push=True``, a push) to argus-docs,
and returns the pin values. It never touches dragonhound-site.

Before committing it verifies the bundle again: the files must match the manifest's
``bundle_sha256``, and every file must pass the opsec gate (fail closed, as at export).
Commits use a bot identity and a noreply address, never the maintainer's git identity, and are
only made when the bundle's content hash changed, so a quiet week makes no commit.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from wiki.public import bundle_hash_of, gate, load_denylist

DEFAULT_REPO = "smk762/argus-docs"
DEFAULT_CHECKOUT = Path.home() / ".cache" / "ai-code-auditor" / "argus-docs"
BOT_NAME = "argus-docs-bot"
BOT_EMAIL = "noreply@dragonhound.dev"  # not @users.noreply.github.com: that could attribute commits to a stranger's account
BRANCH = "main"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def _remote_url(repo: str) -> str:
    return repo if ("://" in repo or repo.startswith("/") or repo.startswith("git@")) else f"git@github.com:{repo}.git"


def verify_bundle(bundle: Path, *, denylist_path: str | Path | None = None,
                  private_names: list[str] | None = None) -> dict[str, Any]:
    """The bundle's manifest, after checking its hash and gating every file. Raises on failure."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    actual = bundle_hash_of(bundle)
    if manifest.get("bundle_sha256") != actual:
        raise RuntimeError(f"bundle hash mismatch: manifest {manifest.get('bundle_sha256')} vs files {actual}")
    denylist = load_denylist(denylist_path)
    for f in sorted(bundle.rglob("*")):
        if f.is_file() and (hit := gate(f.read_text(encoding="utf-8"), denylist, private_names or [])):
            raise RuntimeError(f"opsec gate failed on {f.relative_to(bundle)}: {hit[0]}")
    return manifest


def _private_names(visibility: Callable[[str], str | None] | None) -> list[str]:
    from auditor.config import load_ecosystem_config
    from auditor.merge_poll import github_token
    from wiki.public import github_visibility

    token = github_token()
    lookup = visibility or (lambda r: github_visibility(r, token))
    return sorted(r.name for r in load_ecosystem_config().repos
                  if r.enabled and not r.name.startswith("argus-") and lookup(r.name) != "public")


def publish(bundle_dir: str | Path, *, repo: str = DEFAULT_REPO, checkout: str | Path | None = None,
            push: bool = False, denylist_path: str | Path | None = None,
            visibility: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    bundle = Path(bundle_dir)
    manifest = verify_bundle(bundle, denylist_path=denylist_path, private_names=_private_names(visibility))
    co = Path(checkout or os.getenv("AI_AUDIT_DOCS_CHECKOUT") or DEFAULT_CHECKOUT)

    # a dedicated, machine-managed checkout: brought to the remote's tip, local edits discarded
    if not (co / ".git").is_dir():
        co.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", _remote_url(repo), str(co)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"clone of {repo} failed: {r.stderr.strip()[:400]}")
    else:
        _git(co, "fetch", "-q", "origin")
    has_remote_branch = bool(_git(co, "ls-remote", "--heads", "origin", BRANCH, check=False))
    _git(co, "checkout", "-q", "-B", BRANCH, *([f"origin/{BRANCH}"] if has_remote_branch else []))
    if has_remote_branch:
        _git(co, "reset", "-q", "--hard", f"origin/{BRANCH}")

    try:
        current = json.loads((co / "manifest.json").read_text(encoding="utf-8")).get("bundle_sha256")
    except (OSError, ValueError):
        current = None
    result: dict[str, Any] = {"repo": repo, "checkout": str(co), "bundle_sha256": manifest["bundle_sha256"],
                              "repos": sorted(manifest.get("repos") or {}), "changed": current != manifest["bundle_sha256"],
                              "pushed": False}
    if result["changed"]:
        for child in co.iterdir():  # replace the content wholesale (removed repos/pages disappear)
            if child.name == ".git":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        for f in bundle.rglob("*"):
            if f.is_file():
                dest = co / f.relative_to(bundle)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
        _git(co, "add", "-A")
        lines = "\n".join(f"- {name} @ {str(info.get('sha') or '')[:10]}" for name, info in sorted(manifest["repos"].items()))
        message = (f"docs: export {manifest.get('generated_at', '')[:10]} ({len(manifest['repos'])} repos)\n\n"
                   f"bundle_sha256: {manifest['bundle_sha256']}\n\n{lines}\n")
        _git(co, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q", "-m", message)
    result["commit"] = _git(co, "rev-parse", "HEAD", check=False) or None
    if push and result["changed"]:
        _git(co, "push", "-q", "origin", f"HEAD:{BRANCH}")
        result["pushed"] = True
    result["pin"] = {"repo": repo, "commit": result["commit"], "bundle_sha256": manifest["bundle_sha256"]}
    return result
