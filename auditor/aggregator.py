from __future__ import annotations

from collections import Counter

from auditor.contracts import Finding


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, int, str]] = set()
    deduped: list[Finding] = []
    for finding in findings:
        key = (finding.file_path, finding.type, finding.line, finding.title.lower())
        if key in seen:
            continue
        seen.add(key)
        finding.validate()
        deduped.append(finding)
    return deduped


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counter = Counter(f.severity for f in findings)
    return {k: counter.get(k, 0) for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")}
