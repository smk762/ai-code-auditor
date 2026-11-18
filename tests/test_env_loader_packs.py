from __future__ import annotations

from pathlib import Path

import pytest

from auditor.env_loader import load_env_file


def test_export_prefix_and_quote_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / ".env").write_text(
        "export PACKH_EXPORTED=one\n"
        "PACKH_TRAILING_QUOTE=abc\"\n"
        "PACKH_MISMATCHED=\"abc'\n"
        "PACKH_QUOTED=\"two words\"\n"
        "PACKH_SINGLE='x'\n"
        "PACKH_NESTED=\"'inner'\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    names = ["PACKH_EXPORTED", "PACKH_TRAILING_QUOTE", "PACKH_MISMATCHED", "PACKH_QUOTED", "PACKH_SINGLE",
             "PACKH_NESTED", "export PACKH_EXPORTED"]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    load_env_file()
    import os

    try:
        assert os.environ.get("PACKH_EXPORTED") == "one"
        assert "export PACKH_EXPORTED" not in os.environ
        assert os.environ["PACKH_TRAILING_QUOTE"] == 'abc"'
        assert os.environ["PACKH_MISMATCHED"] == "\"abc'"
        assert os.environ["PACKH_QUOTED"] == "two words"
        assert os.environ["PACKH_SINGLE"] == "x"
        assert os.environ["PACKH_NESTED"] == "'inner'"
    finally:
        for name in names:
            os.environ.pop(name, None)
