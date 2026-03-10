from auditor.contracts import Finding


def test_finding_validates_severity() -> None:
    finding = Finding(
        id="1",
        type="security",
        severity="HIGH",
        repo="repo",
        file_path="a.py",
        line=10,
        title="x",
        description="y",
        evidence="z",
        recommendation="fix",
        source="unit-test",
    )
    finding.validate()


def test_finding_invalid_severity_raises() -> None:
    finding = Finding(
        id="2",
        type="security",
        severity="BAD",
        repo="repo",
        file_path="a.py",
        line=10,
        title="x",
        description="y",
        evidence="z",
        recommendation="fix",
        source="unit-test",
    )
    try:
        finding.validate()
        assert False, "expected ValueError"
    except ValueError:
        assert True
