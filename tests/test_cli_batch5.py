"""cli: the repair autonomy gate judges the pack's own repo, a broken repair stream is a clear
exit, and the merge commands and ``autonomy --refresh`` need an admin JWT when auth is on."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli


def _repair_env(monkeypatch, tmp_path: Path, sent: list) -> None:
    import requests

    from auditor import agent_pack

    monkeypatch.delenv("AI_AUDIT_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_ecosystem_config",
                        lambda: SimpleNamespace(auth_mode=False, auth_code_env="X", output_dir=str(tmp_path)))
    monkeypatch.setattr(cli, "require_auth", lambda *a, **k: None)
    monkeypatch.setattr(agent_pack, "load_pack",
                        lambda out, pid: {"id": pid, "repo": {"name": "argus-locked"},
                                          "location": {"file_path": "app/x.py"}, "narrative": {"title": "t"}})
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(k) or (_ for _ in ()).throw(
        requests.exceptions.ConnectionError("no")))


def test_a_repo_that_is_not_the_packs_is_refused(monkeypatch, tmp_path: Path) -> None:
    sent: list = []
    _repair_env(monkeypatch, tmp_path, sent)
    with pytest.raises(SystemExit, match="does not match"):
        cli._run_repair(repo_name="argus-open", pack_id="pack-x", repair_api_url="http://rag.test",
                        apply=True, push=True, rag_chat_api_key="k")
    assert sent == []


def test_the_gate_judges_the_packs_repo(monkeypatch, tmp_path: Path) -> None:
    from auditor import autonomy

    sent: list = []
    _repair_env(monkeypatch, tmp_path, sent)
    asked: list = []

    def fake_decide(repo, cls, **kw):
        asked.append(repo)
        return autonomy.Decision("report", "pinned", f"{repo}/x", False)

    monkeypatch.setattr(autonomy, "decide", fake_decide)
    with pytest.raises(SystemExit, match="report"):
        cli._run_repair(repo_name="", pack_id="pack-x", repair_api_url="http://rag.test",
                        apply=True, push=True, rag_chat_api_key="k")
    assert asked == ["argus-locked"] and sent == []


class _BrokenStream:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = True):
        import requests

        yield "event: started"
        raise requests.exceptions.ReadTimeout("read timed out")


def test_a_broken_stream_is_a_clear_exit(monkeypatch) -> None:
    import requests

    seen: dict = {}

    def fake_post(url, **kw):
        seen.update(kw)
        return _BrokenStream()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(cli, "load_ecosystem_config",
                        lambda: SimpleNamespace(auth_mode=False, auth_code_env="X", output_dir="/nonexistent"))
    monkeypatch.setattr(cli, "require_auth", lambda *a, **k: None)
    monkeypatch.delenv("AI_AUDIT_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="may still be in progress"):
        cli._run_repair(repo_name="r", repair_compare_branch="feat", repair_api_url="http://rag.test")
    timeout = seen["timeout"]
    assert isinstance(timeout, tuple) and timeout[1] is None


@pytest.mark.parametrize("argv", [["merge-refresh", "--repo", "r", "--now"], ["merge-poll"],
                                  ["autonomy", "--refresh"]], ids=["merge_refresh", "merge_poll", "autonomy"])
def test_merge_commands_need_an_admin_jwt_when_auth_is_on(monkeypatch, argv) -> None:
    import auditor.autonomy
    import auditor.merge_hook
    import auditor.merge_poll

    ran: list = []
    monkeypatch.setattr(sys, "argv", ["ai-audit", *argv])
    monkeypatch.delenv("AI_AUDIT_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli, "load_ecosystem_config",
                        lambda: SimpleNamespace(auth_mode=True, auth_code_env="X", repos=[], output_dir="/x"))
    monkeypatch.setattr(cli, "require_auth", lambda *a, **k: None)
    monkeypatch.setattr(auditor.merge_hook, "run_merge_refresh", lambda *a, **k: ran.append("refresh") or {})
    monkeypatch.setattr(auditor.merge_poll, "poll_dev_tips", lambda *a, **k: ran.append("poll") or {})
    monkeypatch.setattr(auditor.autonomy, "refresh", lambda *a, **k: ran.append("autonomy") or {})
    with pytest.raises(PermissionError, match="admin JWT"):
        cli.main()
    assert ran == []
