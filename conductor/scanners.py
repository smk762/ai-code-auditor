"""Pinned scanner runner + SARIF → Finding adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
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


FINGERPRINT_HEX = 16  # 64 bits: 8 hex (32 bits) collides at ~1% by ~9k keys across the suite
LEGACY_FINGERPRINT_HEX = 8


def fingerprint(category: str, file: str, symbol: str) -> str:
    key = f"{category}|{file}|{symbol}"
    return hashlib.sha256(key.encode()).hexdigest()[:FINGERPRINT_HEX]


def run_scanners(repo_path: str | Path, output_dir: str | Path) -> ScannerResult:
    repo_path = Path(repo_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve().parents[1] / "scripts" / "security" / "run-scanners.sh"
    env = os.environ.copy()
    env["SECURITY_OUT"] = str(output_dir.resolve())
    errors: list[str] = []
    run_rc: int | None = None
    # Outputs of an earlier run must not stand in for this one, whether it fails or the
    # script is missing.
    for name in (*(f"{t}.sarif" for t in SCANNER_TOOLS), "exit-codes.txt", "tool-versions.txt"):
        (output_dir / name).unlink(missing_ok=True)
    if script.exists():
        log_path = output_dir / "run-scanners.log"
        try:
            with log_path.open("w", encoding="utf-8") as log:
                run_rc = _run_in_group(
                    ["bash", str(script), str(repo_path)],
                    env=env,
                    stdout=log,
                    timeout=_timeout_s(),
                )
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
        # A scan that never ran must not read as a clean one.
        errors.append("run-scanners.sh missing")
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


def _run_in_group(args: list[str], *, env: dict[str, str], stdout: Any, timeout: int) -> int:
    """Run *args* in its own process group and return its exit code. On a timeout (or any
    interruption) the whole group is killed, so scanners the script started don't outlive
    it and write into the output dir after this run has returned."""
    proc = subprocess.Popen(
        args, env=env, stdout=stdout, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout)
    except BaseException:
        _kill_group(proc)
        raise


def _kill_group(proc: subprocess.Popen[Any]) -> None:
    """SIGTERM the process group, give it a moment, then SIGKILL whatever is left."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def sarif_to_findings(path: Path, *, source: str, repo: str) -> list[Finding]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    runs = data.get("runs")
    out: list[Finding] = []
    seen: dict[str, int] = {}
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        rule_list = _list(_dict(_dict(run.get("tool")).get("driver")).get("rules"))
        rules = {r.get("id"): r for r in rule_list if isinstance(r, dict) and r.get("id")}
        for result in _list(run.get("results")):
            if not isinstance(result, dict):
                continue
            rule_id = _result_rule_id(result, rule_list)
            # SARIF 2.1.0 §3.27.10: a result without a level takes its rule's
            # defaultConfiguration.level, and only then "warning".
            rule_level = _dict(_dict(rules.get(rule_id)).get("defaultConfiguration")).get("level")
            level = str(result.get("level") or rule_level or "warning").lower()
            severity = {
                "error": "HIGH",
                "warning": "MEDIUM",
                "note": "LOW",
                "none": "INFO",
            }.get(level, "MEDIUM")
            msg = _dict(result.get("message"))
            title = str(msg.get("text") or rule_id)[:200]
            loc = (_list(result.get("locations")) or [{}])[0]
            phys = _dict(_dict(loc).get("physicalLocation"))
            art = _dict(phys.get("artifactLocation"))
            region = _dict(phys.get("region"))
            file_path = str(art.get("uri") or "")
            line = _int_or_zero(region.get("startLine"))
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


def _result_rule_id(result: dict[str, Any], rule_list: list[Any]) -> str:
    """A result's rule: ``ruleId``, else ``rule.id``, else the driver rule at ``ruleIndex``
    (or ``rule.index``), else 'unknown'."""
    ref = _dict(result.get("rule"))
    rid = result.get("ruleId") or ref.get("id")
    if rid:
        return str(rid)
    index = result.get("ruleIndex", ref.get("index"))
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(rule_list):
        rid = _dict(rule_list[index]).get("id")
        if rid:
            return str(rid)
    return "unknown"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
        # Dispositions written before fingerprints grew to 64 bits still match on their prefix.
        disp = dispositions.get(fp) or dispositions.get(fp[:LEGACY_FINGERPRINT_HEX])
        if disp and str(disp.get("decision", "")).lower() in {"rejected", "risk-accepted"}:
            continue
        kept.append(f)
    return kept
