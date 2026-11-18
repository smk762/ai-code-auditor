"""Regression tests for auditor/runtime.py (log path containment, level parsing)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from auditor import runtime


def test_absolute_log_path_with_dotdot_cannot_escape(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    escape = f"{out}/../../etc/cron.d/x"
    with pytest.raises(ValueError):
        runtime._safe_log_path(escape, str(out))


def test_absolute_log_path_through_a_symlink_cannot_escape(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (out / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        runtime._safe_log_path(str(out / "link" / "evil.log"), str(out))


def test_output_dir_itself_is_not_a_log_file(tmp_path: Path):
    with pytest.raises(ValueError):
        runtime._safe_log_path(str(tmp_path), str(tmp_path))


def test_paths_inside_output_dir_are_accepted(tmp_path: Path):
    assert runtime._safe_log_path("logs/run.log", str(tmp_path)) == tmp_path.resolve() / "logs" / "run.log"
    inside = tmp_path / "a.log"
    assert runtime._safe_log_path(str(inside), str(tmp_path)) == inside.resolve()


@pytest.mark.parametrize("level", ["basicconfig", "basic_format", "nonsense"])
def test_non_level_names_fall_back_to_info(monkeypatch, level):
    seen = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    runtime.setup_logging(level=level)
    assert seen["level"] == logging.INFO


def test_real_level_names_are_honoured(monkeypatch):
    seen = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    runtime.setup_logging(level="debug")
    assert seen["level"] == logging.DEBUG
