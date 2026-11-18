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
