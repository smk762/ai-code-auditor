from auditor.aggregator import dedupe_findings
from auditor.contracts import Finding


def _finding(fid: str) -> Finding:
    return Finding(
        id=fid,
        type="code_quality",
        severity="LOW",
        repo="r",
        file_path="x.py",
        line=1,
        title="t",
        description="d",
        evidence="e",
        recommendation="r",
        source="s",
    )


def test_dedupe_findings() -> None:
    out = dedupe_findings([_finding("1"), _finding("2")])
    assert len(out) == 1
