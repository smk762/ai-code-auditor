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
    # Non-empty when the scanner run itself failed (crash, timeout, missing output), so
    # callers can tell "no findings" from "the scan did not run".
    error: str = ""


SCANNER_TOOLS = ("semgrep", "gitleaks", "osv")
DEFAULT_TIMEOUT_S = 1800


def _timeout_s() -> int:
    try:
        return max(1, int(os.getenv("AI_AUDIT_SCANNER_TIMEOUT_S", str(DEFAULT_TIMEOUT_S))))
    except ValueError:
        return DEFAULT_TIMEOUT_S


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
    errors: list[str] = []
    run_rc: int | None = None
    if script.exists():
        # Outputs of an earlier run must not stand in for this one if it fails.
        for name in (*(f"{t}.sarif" for t in SCANNER_TOOLS), "exit-codes.txt"):
            (output_dir / name).unlink(missing_ok=True)
        log_path = output_dir / "run-scanners.log"
        try:
            with log_path.open("w", encoding="utf-8") as log:
                proc = subprocess.run(
                    ["bash", str(script), str(repo_path)],
                    check=False,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=_timeout_s(),
                )
            run_rc = proc.returncode
            if run_rc != 0:
                errors.append(f"run-scanners.sh exited {run_rc}")
        except subprocess.TimeoutExpired:
            run_rc = 124
            errors.append(f"run-scanners.sh timed out after {_timeout_s()}s")
        if errors:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.exists() else ""
            logger.error("%s on %s: %s", errors[-1], repo_path, tail)
        for tool in SCANNER_TOOLS:
            if not (output_dir / f"{tool}.sarif").exists():
                errors.append(f"{tool}.sarif missing")
        if not (output_dir / "exit-codes.txt").exists():
            errors.append("exit-codes.txt missing")
    else:
        logger.warning("run-scanners.sh missing; writing empty SARIF")
        for name in SCANNER_TOOLS:
            (output_dir / f"{name}.sarif").write_text(
                '{"version":"2.1.0","runs":[]}\n', encoding="utf-8"
            )

    findings: list[Finding] = []
    counts: dict[str, int] = {}
    for tool in SCANNER_TOOLS:
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
    if run_rc is not None and (run_rc != 0 or errors):
        exit_codes["run-scanners"] = run_rc if run_rc != 0 else 1
    if errors:
        logger.warning("scanner run on %s incomplete: %s", repo_path, "; ".join(errors))

    return ScannerResult(
        output_dir=output_dir,
        counts=counts,
        findings=findings,
        tool_versions=versions,
        exit_codes=exit_codes,
        error="; ".join(errors),
    )


def sarif_to_findings(path: Path, *, source: str, repo: str) -> list[Finding]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[Finding] = []
    seen: dict[str, int] = {}
    for run in data.get("runs") or []:
        rules = {
            r.get("id"): r
            for r in ((run.get("tool") or {}).get("driver") or {}).get("rules") or []
            if isinstance(r, dict) and r.get("id")
        }
        for result in run.get("results") or []:
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
            # Content-addressed id: stable when other results come and go, and distinct
            # across runs. The ordinal only separates exact duplicates.
            fps = result.get("partialFingerprints") or result.get("fingerprints") or {}
            content = (
                json.dumps(fps, sort_keys=True) if isinstance(fps, dict) and fps
                else f"{line}:{region.get('startColumn') or 0}:{msg.get('text') or ''}"
            )
            base = f"{source}-{rule_id}-{fingerprint(source, file_path, f'{rule_id}|{content}')}"
            n = seen.get(base, 0)
            seen[base] = n + 1
            fid = f"{base}-{n}"
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
