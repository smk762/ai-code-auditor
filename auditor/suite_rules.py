"""Suite rules (#55): the argus-ci lint catalogue, run by the auditor the same way CI runs it.

The catalogue lives in argus-ci (``suite-rules/``): every rule names its finding class (#53) and
has a positive and a negative fixture. Repos opt in through argus-ci's ``suite-rules.yml``
workflow, with a baseline that only shrinks. Here the same ``check.py`` runs as a subprocess
from the argus-ci mirror, so a scan's findings and CI can't disagree:

* ``scan()``: findings for the nightly and diff audits (source ``suite``, id
  ``suite:<rule>:<path>:<line>``, so the class maps straight from the rule id). Convention rules
  (``kind = "convention"``) enforce a code shape, not a class, so they are not findings.
* ``fixtures()``: the ``rules.fixtures`` gate. Every rule must fire on its positive fixture and stay
  silent on its negative one.

The catalogue is ``suite-rules/`` from the argus-ci mirror's ``origin/dev`` (exported to a cache by
tree hash), or ``AI_AUDIT_SUITE_RULES_DIR``.
ruff and semgrep come from ``$RUFF`` / ``$SEMGREP``, else from this environment.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auditor.contracts import Finding
from auditor.repo_paths import mirror_paths

logger = logging.getLogger(__name__)

FIXTURES_NAME = "rules_fixtures.json"
SECURITY_CLASSES = frozenset({"ssrf", "path_traversal", "secret_exposure", "injection", "info_leak", "fail_open"})
_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}
# An older catalogue is pruned only once nothing has used it for this long. Every use touches
# it, and one check.py run is capped at 900s, so a concurrent run's catalogue is never pulled
# out from under it.
PRUNE_GRACE_S = 3600


def catalogue_dir(mirrors_path: str | Path = "config/repo_mirrors.yaml", *,
                  cache_root: str | Path | None = None) -> Path | None:
    """The catalogue as merged on argus-ci's ``dev``: exported from the mirror's ``origin/dev`` into
    ``<output_dir>/_suite_rules/<sha>/``, so whatever branch the mirror has checked out doesn't
    matter. ``AI_AUDIT_SUITE_RULES_DIR`` points at a directory instead."""
    env = os.getenv("AI_AUDIT_SUITE_RULES_DIR")
    if env:
        d = Path(env)
        return d if (d / "check.py").is_file() and (d / "catalogue.toml").is_file() else None
    ci = mirror_paths(mirrors_path).get("argus-ci")
    if ci is None or not (ci / ".git").exists():
        return None
    return export_catalogue(ci, Path(cache_root or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports") / "_suite_rules")


def export_catalogue(mirror: Path, cache: Path) -> Path | None:
    def git(*args: str, **kw: Any) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=mirror, capture_output=True, timeout=60, check=False, **kw)

    for ref in ("origin/dev", "HEAD"):
        r = git("rev-parse", "--verify", "-q", f"{ref}:suite-rules", text=True)
        if r.returncode == 0:
            tree = r.stdout.strip()
            break
    else:
        return None
    dest = cache / tree[:16]
    if (dest / "suite-rules" / "check.py").is_file():
        _touch(dest)
        return dest / "suite-rules"
    # Archive the tree object itself, not the symbolic ref: a fetch moving origin/dev between
    # the two calls would otherwise cache tree Y's files under tree X's name.
    arch = git("archive", "--format=tar", "--prefix=suite-rules/", tree)
    if arch.returncode != 0:
        return None
    import io
    import tarfile

    tmp = cache / f".tmp-{tree[:16]}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(arch.stdout)) as tf:
            tf.extractall(tmp, filter="data")
    except (tarfile.TarError, OSError) as exc:
        # Pruning below skips .tmp-* and the next run has another pid: never leave it behind.
        shutil.rmtree(tmp, ignore_errors=True)
        logger.warning("suite rules export of %s failed: %s", tree[:16], exc)
        return None
    try:
        tmp.rename(dest)
    except OSError:  # another process exported the same tree first
        shutil.rmtree(tmp, ignore_errors=True)
    _touch(dest)
    _prune(cache, keep=dest.name)
    d = dest / "suite-rules"
    return d if (d / "check.py").is_file() else None


def _touch(d: Path) -> None:
    """Mark *d* as in use now (its mtime is what pruning reads)."""
    try:
        os.utime(d)
    except OSError:
        pass


def _prune(cache: Path, *, keep: str) -> None:
    """Drop catalogues other than *keep* that nothing has used for ``PRUNE_GRACE_S``."""
    cutoff = time.time() - PRUNE_GRACE_S
    for old in cache.iterdir():
        if old.name == keep or old.name.startswith(".tmp-"):
            continue
        try:
            if old.stat().st_mtime > cutoff:
                continue  # possibly another run's catalogue, still in use
        except OSError:
            continue
        shutil.rmtree(old, ignore_errors=True)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    here = Path(sys.executable).parent
    for var, tool in (("RUFF", "ruff"), ("SEMGREP", "semgrep")):
        if not env.get(var):
            local = here / tool
            env[var] = str(local) if local.exists() else (shutil.which(tool) or tool)
    return env


def _run(cdir: Path, args: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(cdir / "check.py"), *args], capture_output=True, text=True,
                          timeout=timeout, env=_env(), check=False)


def scan(repo_name: str, repo_root: str | Path, paths: list[str] | None = None, *,
         cdir: Path | None = None) -> list[Finding]:
    """Guard-rule hits in *repo_root* (optionally only *paths*) as findings. Never raises."""
    cdir = cdir or catalogue_dir()
    if cdir is None:
        return []
    root = Path(repo_root)
    try:
        # "--": a repo path starting with "-" is a path, never an option such as --root
        r = _run(cdir, ["scan", "--root", str(root), "--json", "--", *(paths or [])])
        if r.returncode != 0:
            logger.warning("suite rules scan of %s failed: %s", repo_name, r.stderr.strip()[-300:])
            return []
        data = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.warning("suite rules scan of %s failed: %s", repo_name, exc)
        return []
    if not isinstance(data, dict):
        logger.warning("suite rules scan of %s: expected a JSON object, got %s", repo_name, type(data).__name__)
        return []
    meta = data.get("rules") if isinstance(data.get("rules"), dict) else {}
    hits = data.get("hits") if isinstance(data.get("hits"), list) else []
    out = []
    for h in hits:
        # Untrusted tool output: a list/dict rule is unhashable and an int path can't join a Path.
        if not isinstance(h, dict) or not isinstance(h.get("rule"), str) or not h["rule"] \
                or not isinstance(h.get("path"), str) or not h["path"]:
            continue
        m = meta.get(h.get("rule")) or {}
        if not isinstance(m, dict) or not m or m.get("kind", "guard") != "guard":
            continue
        try:
            line = int(h.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        cls = str(m.get("class") or "")
        sev = str(m.get("severity") or "MEDIUM").upper()
        out.append(Finding(
            id=f"suite:{h['rule']}:{h['path']}:{line}",
            type="security" if cls in SECURITY_CLASSES else "bug",
            severity=sev if sev in _SEVERITIES else "MEDIUM",
            repo=repo_name,
            file_path=str(root / h["path"]),
            line=line,
            title=f"{h['rule']}: {str(m.get('message') or '').split('. ')[0].rstrip('.')}",
            description=str(m.get("message") or "") + (f" ({h['detail']})" if h.get("detail") else ""),
            evidence=str(h.get("text") or ""),
            recommendation=(f"{m.get('message', '')} Suite rule `{h['rule']}` (argus-ci suite-rules, class {cls}); "
                            f"a reasoned `# noqa` / `# nosemgrep: {h['rule']}` silences one line."),
            source="suite",
        ))
    return out


def fixtures(output_dir: str | Path | None = None, *, cdir: Path | None = None) -> dict[str, Any] | None:
    """Run the catalogue's fixture proofs; writes ``rules_fixtures.json``. None without a catalogue."""
    cdir = cdir or catalogue_dir()
    if cdir is None:
        return None
    try:
        r = _run(cdir, ["fixtures", "--json"])
        rules = json.loads(r.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        rules = []
        note = f"fixtures did not run: {exc}"
    else:
        if not isinstance(rules, list):  # e.g. null or {"error": ...}: a degraded report, not a crash
            note = f"unexpected fixtures output: {type(rules).__name__}"
            rules = []
        else:
            rules = [x for x in rules if isinstance(x, dict)]
            note = "" if rules else f"fixtures produced no results: {r.stderr.strip()[-200:]}"
    rep = {"at": datetime.now(UTC).isoformat(), "catalogue": str(cdir), "n": len(rules),
           "ok": sum(1 for x in rules if x.get("ok")), "rules": rules, "note": note}
    rep["rate"] = round(rep["ok"] / rep["n"], 4) if rep["n"] else None
    if output_dir is not None:
        (Path(output_dir) / FIXTURES_NAME).write_text(json.dumps(rep, indent=1))
    return rep
