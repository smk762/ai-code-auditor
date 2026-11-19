"""scripts/model_eval.py exits cleanly when the sample is empty."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(monkeypatch):
    spec = importlib.util.spec_from_file_location("model_eval_script", ROOT / "scripts" / "model_eval.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)  # its dataclasses look the module up
    spec.loader.exec_module(mod)
    return mod


def test_empty_sample_exits_with_a_message(tmp_path: Path, monkeypatch, capsys) -> None:
    mod = _load(monkeypatch)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "sample_units", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["model_eval.py", "--n", "0", "--dry-run"])
    assert mod.main() == 1
    assert "nothing to evaluate" in capsys.readouterr().err
