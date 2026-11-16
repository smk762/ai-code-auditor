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

The catalogue is found at ``AI_AUDIT_SUITE_RULES_DIR``, else ``<argus-ci mirror>/suite-rules``.
ruff and semgrep come from ``$RUFF`` / ``$SEMGREP``, else from this environment.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auditor.contracts import Finding

logger = logging.getLogger(__name__)

FIXTURES_NAME = "rules_fixtures.json"
SECURITY_CLASSES = frozenset({"ssrf", "path_traversal", "secret_exposure", "injection", "info_leak", "fail_open"})
_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}


def catalogue_dir(mirrors_path: str | Path = "config/repo_mirrors.yaml") -> Path | None:
    env = os.getenv("AI_AUDIT_SUITE_RULES_DIR")
    if env:
        d = Path(env)
    else:
        from auditor.trust_metrics import _mirror_paths

        ci = _mirror_paths(mirrors_path).get("argus-ci")
        if ci is None:
            return None
        d = ci / "suite-rules"
    return d if (d / "check.py").is_file() and (d / "catalogue.toml").is_file() else None


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
        r = _run(cdir, ["scan", "--root", str(root), "--json", *(paths or [])])
        if r.returncode != 0:
            logger.warning("suite rules scan of %s failed: %s", repo_name, r.stderr.strip()[-300:])
            return []
        data = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.warning("suite rules scan of %s failed: %s", repo_name, exc)
        return []
    meta = data.get("rules") or {}
    out = []
    for h in data.get("hits") or []:
        m = meta.get(h.get("rule")) or {}
        if not m or m.get("kind", "guard") != "guard":
            continue
        cls = str(m.get("class") or "")
        sev = str(m.get("severity") or "MEDIUM").upper()
        out.append(Finding(
            id=f"suite:{h['rule']}:{h['path']}:{h['line']}",
            type="security" if cls in SECURITY_CLASSES else "bug",
            severity=sev if sev in _SEVERITIES else "MEDIUM",
            repo=repo_name,
            file_path=str(root / h["path"]),
            line=int(h.get("line") or 0),
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
        note = "" if rules else f"fixtures produced no results: {r.stderr.strip()[-200:]}"
    rep = {"at": datetime.now(UTC).isoformat(), "catalogue": str(cdir), "n": len(rules),
           "ok": sum(1 for x in rules if x.get("ok")), "rules": rules, "note": note}
    rep["rate"] = round(rep["ok"] / rep["n"], 4) if rep["n"] else None
    if output_dir is not None:
        (Path(output_dir) / FIXTURES_NAME).write_text(json.dumps(rep, indent=1))
    return rep
