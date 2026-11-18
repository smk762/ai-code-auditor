"""SSE parsing in ``ai-audit repair`` (``cli._run_repair``)."""
from __future__ import annotations

import cli


def _stream(monkeypatch, lines: list[str]) -> None:
    import requests

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self, decode_unicode: bool = True):
            yield from lines

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())


def test_event_name_does_not_leak_into_the_next_event(monkeypatch, capsys) -> None:
    _stream(monkeypatch, [
        "event: warning",
        'data: {"message": "soft"}',
        "",
        'data: {"final_patch": "patch-body", "applied": true, "branch": "fix/x"}',
        "",
    ])
    cli._run_repair(repo_name="argus-cortex", repair_compare_branch="dev", repair_api_url="http://rag.test")
    out = capsys.readouterr().out
    assert "Warning: soft" in out
    assert "Warning: None" not in out
    assert "Branch:        fix/x" in out and "--- Final patch ---" in out


def test_non_object_json_data_is_printed_not_fatal(monkeypatch, capsys) -> None:
    _stream(monkeypatch, [
        "data: [1, 2]",
        "",
        "data: 42",
        "",
        'data: {"message": "after"}',
        "",
    ])
    cli._run_repair(repo_name="argus-cortex", repair_compare_branch="dev", repair_api_url="http://rag.test")
    out = capsys.readouterr().out
    assert "data: [1, 2]" in out and "data: 42" in out
    assert "Error: after" in out
