from __future__ import annotations

from pathlib import Path

from patterns.service_role_violations import detect_competing_providers


def test_same_backend_defined_twice_is_not_competing(tmp_path: Path) -> None:
    repo = tmp_path / "argus-x"
    repo.mkdir()
    a = repo / "providers.py"
    a.write_text(
        "try:\n"
        "    class GothmogImageProvider: pass\n"
        "except ImportError:\n"
        "    class GothmogImageProvider: pass\n"
    )
    assert detect_competing_providers({"argus-x": [a]}, []) == []

    b = repo / "local.py"
    b.write_text("class SelfHostedImageProvider: pass\n")
    out = detect_competing_providers({"argus-x": [a, b]}, [])
    assert len(out) == 1
    assert "2 competing ImageProvider implementations (Gothmog, SelfHosted)" in out[0].evidence


def test_an_unparseable_file_is_skipped_not_fatal(tmp_path: Path, monkeypatch) -> None:
    import patterns.service_role_violations as srv

    bad, good = tmp_path / "argus-bad", tmp_path / "argus-good"
    bad.mkdir()
    good.mkdir()
    (bad / "nul.py").write_text("x = 1\n")
    (bad / "deep.py").write_text("x = 2\n")
    (good / "a.py").write_text("class GothmogImageProvider: pass\n")
    (good / "b.py").write_text("class SelfHostedImageProvider: pass\n")
    real_parse = srv.ast.parse

    def parse(source, filename="<unknown>", *a, **k):
        if filename.endswith("nul.py"):  # what Python < 3.12 raises for NUL bytes
            raise ValueError("source code string cannot contain null bytes")
        if filename.endswith("deep.py"):  # pathologically nested generated code
            raise RecursionError("maximum recursion depth exceeded during compilation")
        return real_parse(source, filename, *a, **k)

    monkeypatch.setattr(srv.ast, "parse", parse)
    files = {"argus-bad": sorted(bad.iterdir()), "argus-good": sorted(good.iterdir())}
    out = detect_competing_providers(files, [])
    assert len(out) == 1 and "ImageProvider" in out[0].evidence
