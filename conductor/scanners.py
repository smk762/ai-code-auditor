"""Pinned scanner runner + SARIF → Finding adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auditor.contracts import Finding

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ScannerResult:
    output_dir: Path
    counts: dict[str, int]
    findings: list[Finding]
    tool_versions: str
    exit_codes: dict[str, int]


def fingerprint(category: str, file: str, symbol: str) -> str:
    key = f"{category}|{file}|{symbol}"
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def run_scanners(repo_path: str | Path, output_dir: str | Path) -> ScannerResult:
    repo_path = Path(repo_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve().parents[1] / "scripts" / "security" / "run-scanners.sh"
    env = os.environ.copy()
    env["SECURITY_OUT"] = str(output_dir.resolve())
    if script.exists():
        subprocess.run(
            ["bash", str(script), str(repo_path)],
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
    else:
        logger.warning("run-scanners.sh missing; writing empty SARIF")
        for name in ("semgrep", "gitleaks", "osv"):
            (output_dir / f"{name}.sarif").write_text(
                '{"version":"2.1.0","runs":[]}\n', encoding="utf-8"
            )

    findings: list[Finding] = []
    counts: dict[str, int] = {}
    for tool in ("semgrep", "gitleaks", "osv"):
        path = output_dir / f"{tool}.sarif"
        tool_findings = sarif_to_findings(path, source=tool, repo=repo_path.name)
        counts[tool] = len(tool_findings)
        findings.extend(tool_findings)

    versions = ""
    vp = output_dir / "tool-versions.txt"
    if vp.exists():
        versions = vp.read_text(encoding="utf-8")

    exit_codes: dict[str, int] = {}
    ep = output_dir / "exit-codes.txt"
    if ep.exists():
        for line in ep.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                try:
                    exit_codes[k.strip()] = int(v.strip())
                except ValueError:
                    pass

    return ScannerResult(
        output_dir=output_dir,
        counts=counts,
        findings=findings,
        tool_versions=versions,
        exit_codes=exit_codes,
    )


def sarif_to_findings(path: Path, *, source: str, repo: str) -> list[Finding]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[Finding] = []
    for run in data.get("runs") or []:
        rules = {
            r.get("id"): r
            for r in ((run.get("tool") or {}).get("driver") or {}).get("rules") or []
            if isinstance(r, dict) and r.get("id")
        }
        for i, result in enumerate(run.get("results") or []):
            if not isinstance(result, dict):
                continue
            rule_id = str(result.get("ruleId") or "unknown")
            level = str(result.get("level") or "warning").lower()
            severity = {
                "error": "HIGH",
                "warning": "MEDIUM",
                "note": "LOW",
                "none": "INFO",
            }.get(level, "MEDIUM")
            msg = result.get("message") or {}
            title = str(msg.get("text") or rule_id)[:200]
            loc = (result.get("locations") or [{}])[0]
            phys = (loc.get("physicalLocation") or {}) if isinstance(loc, dict) else {}
            art = phys.get("artifactLocation") or {}
            region = phys.get("region") or {}
            file_path = str(art.get("uri") or "")
            line = int(region.get("startLine") or 0)
            fid = f"{source}-{rule_id}-{fingerprint(source, file_path, rule_id)}-{i}"
            out.append(
                Finding(
                    id=fid,
                    type=source,
                    severity=severity,
                    repo=repo,
                    file_path=file_path,
                    line=line,
                    title=title,
                    description=title,
                    evidence=json.dumps({"rule": rule_id, "rule_meta": rules.get(rule_id)}, default=str)[:2000],
                    recommendation=f"Review {source} finding {rule_id}",
                    source=source,
                )
            )
    return out


def load_dispositions(path: str | Path = "security/dispositions.yaml") -> dict[str, dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return {}
    import yaml

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if isinstance(raw, dict):
        raw = raw.get("dispositions") or []
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, dict) and item.get("fingerprint"):
            out[str(item["fingerprint"])] = item
    return out


def filter_by_dispositions(
    findings: list[Finding],
    dispositions: dict[str, dict[str, Any]],
) -> list[Finding]:
    kept: list[Finding] = []
    for f in findings:
        fp = fingerprint(f.type or f.source, f.file_path, f.title[:80])
        disp = dispositions.get(fp)
        if disp and str(disp.get("decision", "")).lower() in {"rejected", "risk-accepted"}:
            continue
        kept.append(f)
    return kept
