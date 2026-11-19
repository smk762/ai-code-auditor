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


@pytest.fixture
def _restore_root_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for h in root.handlers[:]:
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)


def test_second_setup_logging_call_takes_effect(tmp_path: Path, _restore_root_logging):
    runtime.setup_logging(level="INFO", log_file_path="first.log", output_dir=str(tmp_path))
    first = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    runtime.setup_logging(level="DEBUG", log_file_path="second.log", output_dir=str(tmp_path))
    logging.getLogger("packs.runtime").debug("hello-second")
    for h in logging.getLogger().handlers:
        h.flush()
    assert logging.getLogger().level == logging.DEBUG
    assert "hello-second" in (tmp_path / "second.log").read_text()
    assert all(h.stream is None for h in first)  # the first file handler was closed, not leaked


def test_auth_code_is_compared_in_constant_time(monkeypatch):
    import hmac

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    monkeypatch.setenv("PACKS_AUTH", "s3cret")
    runtime.require_auth(True, "PACKS_AUTH", "s3cret")
    with pytest.raises(PermissionError):
        runtime.require_auth(True, "PACKS_AUTH", "s3creX")
    with pytest.raises(PermissionError):
        runtime.require_auth(True, "PACKS_AUTH", "pässword")
    assert len(calls) == 3
