from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor.config import RepoConfig
from auditor.spend_ledger import BudgetExceeded, get_ledger
from wiki import citations, notes as notes_mod
from wiki.batch import Request, Runner, request_id
from wiki.sources import file_structure

APP = '''"""Tiny service."""
import os

from fastapi import FastAPI

__all__ = ["create_app", "load_settings"]


def create_app():
    app = FastAPI()
    return app


def load_settings():
    import yaml
    return yaml.safe_load(os.environ.get("CFG", "{}"))
'''


# ── fake Anthropic client ────────────────────────────────────────────────────

def _msg(text: str, model: str, stop: str = "end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, model=model,
                           usage=SimpleNamespace(input_tokens=2000, output_tokens=500,
                                                 cache_creation_input_tokens=0, cache_read_input_tokens=0))


def responder(params: dict) -> str:
    """What the fake model replies, by request kind."""
    sys0 = params["system"][0]["text"] if params.get("system") else ""
    if params["model"].startswith("claude-haiku"):
        return json.dumps({"summary": "Builds the FastAPI app and loads settings.", "role": "entrypoint",
                           "key_symbols": [{"name": "create_app", "kind": "function", "start_line": 9,
                                            "end_line": 11, "purpose": "app factory"}],
                           "interfaces": ["env CFG"], "dependencies": ["fastapi", "yaml"]})
    if sys0.startswith("You write pages"):
        return ("# Page\n\n"
                "The app is built by `create_app` [app.py:L9-L11]. It reads `CFG` [app.py:L14-L16].\n\n"
                "- `load_settings` lives in a file that does not exist [nope.py:L1-L2]\n"
                "- Settings come from `load_settings` [app.py:L14-L16]\n")
    content = params["messages"][0]["content"]
    return content.split("## Draft\n", 1)[1]  # check-and-fix: keep the draft as is


class FakeClient:
    def __init__(self, reply=responder, fail_ids: set[str] | None = None):
        self.reply, self.fail_ids = reply, fail_ids or set()
        self.batches_created: list[list[dict]] = []
        self.streamed = 0
        self.pending: dict[str, list[dict]] = {}
        outer = self

        class _Batches:
            def create(self, requests):
                bid = f"msgbatch_{len(outer.batches_created)}"
                outer.batches_created.append(requests)
                outer.pending[bid] = requests
                return SimpleNamespace(id=bid, processing_status="in_progress")

            def retrieve(self, bid):
                return SimpleNamespace(processing_status="ended", request_counts=SimpleNamespace(processing=0))

            def results(self, bid):
                for r in outer.pending.pop(bid, []):
                    if r["custom_id"] in outer.fail_ids:
                        res = SimpleNamespace(type="errored", error=SimpleNamespace(type="api_error"))
                    else:
                        res = SimpleNamespace(type="succeeded", message=_msg(outer.reply(r["params"]), r["params"]["model"]))
                    yield SimpleNamespace(custom_id=r["custom_id"], result=res)

        class _Stream:
            def __init__(self, params):
                self.params = params

            def __enter__(self):
                outer.streamed += 1
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return _msg(outer.reply(self.params), self.params["model"])

        self.messages = SimpleNamespace(batches=_Batches(), stream=lambda **p: _Stream(p))


def _req(i: int, model="claude-haiku-5-5", est=0.01) -> Request:
    params = {"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": f"q{i}"}]}
    return Request(request_id("t", params), params, est, "wiki:test")


# ── sources / notes ──────────────────────────────────────────────────────────

def test_structure_facts_python_and_js(tmp_path: Path) -> None:
    s = file_structure(Path("app.py"), APP)
    assert s["exports"] == ["create_app", "load_settings"]
    assert s["top_level"] == ["create_app", "load_settings"]
    assert "fastapi: FastAPI" in s["imports"] and s["lazy_imports"] == ["yaml"]
    js = file_structure(Path("a.ts"), "import x from './x'\nexport function go() {}\nconst y = require('y')\n")
    assert js["imports"] == ["./x", "y"] and js["exports"] == ["go"]


def test_note_key_matches_eval_cache_and_legacy_notes_are_reused(tmp_path: Path) -> None:
    import hashlib

    assert notes_mod.note_key("app.py", APP) == hashlib.sha1(f"app.py\0{APP}".encode()).hexdigest()[:16]
    repo_dir = tmp_path / "svc"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text(APP)
    repo = RepoConfig("svc", str(repo_dir))
    out = tmp_path / "reports"
    jobs, cached, _ = notes_mod.scan_notes(repo, out)
    assert len(jobs) == 1 and cached == 0
    legacy = notes_mod.legacy_notes_dir(out) / "svc" / f"{jobs[0].key}.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"path": "app.py", "lines": 16, "summary": "s", "role": "r",
                                  "key_symbols": [], "interfaces": [], "dependencies": []}))
    jobs, cached, _ = notes_mod.scan_notes(repo, out)
    assert jobs == [] and cached == 1
    assert notes_mod.load_notes(repo, out)[0]["summary"] == "s"


def test_save_note_rejects_bad_replies(tmp_path: Path) -> None:
    job = notes_mod.NoteJob("svc", "app.py", APP, "k1")
    assert notes_mod.save_note(job, "not json", tmp_path) is False
    assert notes_mod.save_note(job, json.dumps({"summary": "x"}), tmp_path) is False
    assert notes_mod.save_note(job, responder({"model": "claude-haiku-5-5", "messages": []}), tmp_path) is True
    assert len(job.custom_id) <= 64


# ── citations ────────────────────────────────────────────────────────────────

@pytest.fixture
def code(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(APP)
    (tmp_path.parent / "secret.py").write_text("x = 1\n")
    return tmp_path


def test_check_classifies_citations(code: Path) -> None:
    md = ("`create_app` builds it [app.py:L9-L11]. `load_settings` is here [app.py:L9-L11]. "
          "Missing [nope.py:L1]. Out of range [app.py:L90-L95]. Escape [../secret.py:L1].")
    rep = citations.check(md, code)
    assert (rep.total, rep.valid, rep.named, rep.matched) == (5, 2, 2, 1)
    assert [f["reason"] for f in rep.failed] == ["symbol", "missing", "range", "missing"]


def test_drop_failed_claims(code: Path) -> None:
    md = ("# Title\n\nIt has `create_app` [app.py:L9-L11]. It also has `ghost` [app.py:L9-L11]. And more.\n\n"
          "- good item [app.py:L1-L2]\n- bad item [nope.py:L1]\n\n"
          "| Route | Where |\n|---|---|\n| GET / | [app.py:L200] |\n\n"
          "```python\nx = '[nope.py:L1]'\n```\n")
    out, dropped = citations.drop_failed_claims(md, code)
    assert "It has `create_app` [app.py:L9-L11]. And more." in out
    assert "ghost" not in out and "bad item" not in out and "GET /" not in out
    assert "- good item [app.py:L1-L2]" in out and "x = '[nope.py:L1]'" in out  # code fence untouched
    assert sorted(d["reason"] for d in dropped) == ["missing", "range", "symbol"]


def test_cited_files_and_code(code: Path) -> None:
    md = "a [app.py:L9-L10] b [app.py:L11] c [nope.py:L1]"
    assert list(citations.cited_files(md, code)) == ["app.py"]
    shown = citations.cited_code(md, code, pad=0)
    assert "app.py L9-L11" in shown and "nope.py (FILE DOES NOT EXIST)" in shown  # adjacent ranges merged


# ── batch runner ─────────────────────────────────────────────────────────────

def test_runner_batches_records_spend_and_reuses_results(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "answer")
    runner = Runner(tmp_path, client=fake, poll_s=0)
    reqs = [_req(1), _req(2)]
    results, st = runner.run("t", reqs)
    assert all(r.ok and r.text == "answer" for r in results.values())
    assert st.submitted == 2 and len(fake.batches_created) == 1 and st.cost_usd > 0
    assert get_ledger().spent("wiki") == pytest.approx(st.cost_usd)
    # second run: nothing new is submitted
    results2, st2 = Runner(tmp_path, client=fake, poll_s=0).run("t", reqs)
    assert st2.reused == 2 and st2.submitted == 0 and len(fake.batches_created) == 1
    runner.forget([r.custom_id for r in reqs])
    _, st3 = runner.run("t", reqs)
    assert st3.submitted == 2


def test_runner_harvests_batches_from_an_interrupted_run(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "late answer")
    runner = Runner(tmp_path, client=fake, poll_s=0)
    runner._harvest = lambda *a, **k: False  # simulate dying before the batch ends
    reqs = [_req(1)]
    runner.run("t", reqs)
    assert json.loads((tmp_path / "wiki" / "_batches.json").read_text())["batches"]
    fresh = Runner(tmp_path, client=fake, poll_s=0)
    results, st = fresh.run("t", reqs)
    assert st.submitted == 0 and st.reused == 1 and results[reqs[0].custom_id].text == "late answer"
    assert len(fake.batches_created) == 1  # harvested, never resubmitted


def test_runner_retries_errored_results_and_checks_budget(tmp_path: Path, monkeypatch) -> None:
    reqs = [_req(1), _req(2)]
    fake = FakeClient(reply=lambda p: "ok", fail_ids={reqs[1].custom_id})
    results, st = Runner(tmp_path, client=fake, poll_s=0).run("t", reqs)
    assert st.failed == 1 and results[reqs[1].custom_id].error
    fake.fail_ids.clear()
    _, st2 = Runner(tmp_path, client=fake, poll_s=0).run("t", reqs)
    assert st2.reused == 1 and st2.submitted == 1

    monkeypatch.setenv("AI_AUDIT_BUDGET_WIKI_USD", "0.5")
    with pytest.raises(BudgetExceeded):
        Runner(tmp_path, client=fake, poll_s=0).run("t", [_req(9, est=1.0)])
    assert len(fake.batches_created) == 2  # nothing submitted over budget


def test_sync_mode_streams_at_full_price(tmp_path: Path) -> None:
    fake = FakeClient(reply=lambda p: "s")
    results, st = Runner(tmp_path, mode="sync", client=fake).run("t", [_req(1)])
    assert fake.streamed == 1 and not fake.batches_created and next(iter(results.values())).ok
    rows = get_ledger().rows_since(0)
    assert rows[-1][6] == 0  # recorded as a non-batch call


# ── end-to-end build ─────────────────────────────────────────────────────────

@pytest.fixture
def ecosystem(tmp_path: Path, monkeypatch, memdb):
    repo_dir = tmp_path / "mirrors" / "svc"
    repo_dir.mkdir(parents=True)
    (repo_dir / "app.py").write_text(APP)
    (repo_dir / "README.md").write_text("# svc\nA tiny service.\n")

    class _Cfg:
        repos = [RepoConfig("svc", str(repo_dir)), RepoConfig("off", "/x", enabled=False)]
        output_dir = str(tmp_path / "reports")

    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    return tmp_path / "reports"


def test_build_writes_checked_pages_index_and_findings(ecosystem: Path) -> None:
    from wiki.build import build

    fake = FakeClient()
    rep = build(["svc"], ecosystem, runner=Runner(ecosystem, client=fake, poll_s=0))
    r = rep.repos["svc"]
    assert r.notes_written == 1 and sorted(r.pages_written) == ["architecture", "integration", "interfaces", "overview"]
    assert r.dropped_claims == 4  # the nope.py bullet on each of the 4 pages
    assert [s for s in rep.stages] == ["notes", "drafts", "verify"]
    assert len(fake.batches_created) == 3 and rep.cost_usd > 0

    d = ecosystem / "wiki" / "svc"
    page = (d / "overview.md").read_text()
    assert "nope.py" not in page and "`create_app` [app.py:L9-L11]" in page
    idx = json.loads((d / "INDEX.json").read_text())
    assert [p["slug"] for p in idx["pages"]] == ["overview", "architecture", "interfaces", "integration", "findings"]
    ov = idx["pages"][0]
    assert list(ov["cited_files"]) == ["app.py"] and ov["citations"]["failed"] == [] and ov["dropped_claims"]
    assert idx["pages"][-1]["generator"] == "data" and (d / "findings.md").read_text().startswith("# Notable findings")
    top = json.loads((ecosystem / "wiki" / "INDEX.json").read_text())
    assert list(top["repos"]) == ["svc"] and top["repos"]["svc"]["pages"] == 5
    assert not list((ecosystem / "wiki" / "_results").glob("*.json"))  # cleaned up after writing

    # a rebuild reuses the cached notes
    rep2 = build(["svc"], ecosystem, runner=Runner(ecosystem, client=fake, poll_s=0))
    assert rep2.stages["notes"]["requested"] == 0


def test_failed_check_keeps_previous_page_and_draft(ecosystem: Path) -> None:
    from wiki.build import build

    build(["svc"], ecosystem, runner=Runner(ecosystem, client=FakeClient(), poll_s=0))
    before = (ecosystem / "wiki" / "svc" / "overview.md").read_text()

    def reply(p):
        if p.get("system") and p["system"][0]["text"].startswith("You are fact-checking"):
            return "should not ship"
        return responder(p)

    fake = FakeClient(reply=reply)
    original = fake.messages.batches.results

    def results(bid):  # every check-and-fix result errors
        for e in original(bid):
            if e.custom_id.startswith("v-"):
                e = SimpleNamespace(custom_id=e.custom_id, result=SimpleNamespace(type="errored", error=SimpleNamespace(type="overloaded_error")))
            yield e

    fake.messages.batches.results = results
    rep = build(["svc"], ecosystem, runner=Runner(ecosystem, client=fake, poll_s=0))
    assert rep.repos["svc"].pages_written == [] and set(rep.repos["svc"].pages_failed) == {
        "overview", "architecture", "interfaces", "integration"}
    assert (ecosystem / "wiki" / "svc" / "overview.md").read_text() == before
    idx = json.loads((ecosystem / "wiki" / "svc" / "INDEX.json").read_text())
    assert idx["pages"][0]["stale"] is True
    # the drafts were paid for, so they are kept for the next run
    assert len(list((ecosystem / "wiki" / "_results").glob("d-*.json"))) == 4


def test_dry_run_prices_without_calls(ecosystem: Path) -> None:
    from wiki.build import build

    fake = FakeClient()
    rep = build(None, ecosystem, dry_run=True, runner=Runner(ecosystem, client=fake))
    assert rep.dry_run and rep.estimate_usd > 0 and rep.stages["notes"]["requested"] == 1
    assert not fake.batches_created and not (ecosystem / "wiki" / "svc").exists()
    with pytest.raises(ValueError):
        build(["off"], ecosystem, dry_run=True)


def test_build_stops_cleanly_when_over_budget(ecosystem: Path, monkeypatch) -> None:
    from wiki.build import build

    monkeypatch.setenv("AI_AUDIT_BUDGET_WIKI_USD", "0.0001")
    fake = FakeClient()
    rep = build(["svc"], ecosystem, runner=Runner(ecosystem, client=fake, poll_s=0))
    assert "wiki notes" in rep.stopped and not fake.batches_created
