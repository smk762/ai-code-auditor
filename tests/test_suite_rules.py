"""Suite rules (#55): the auditor runs argus-ci's check.py; findings, classes and the gate.

A fake check.py stands in for the real catalogue: the real one shells out to ruff and semgrep,
and its fixtures are proven in argus-ci's own CI and by the rules.fixtures gate."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from auditor import finding_class as fc
from auditor import pack_classes, suite_rules, trust_eval
from auditor.agent_pack import load_pack, packs_dir

SCAN = {
    "rules": {
        "token-compare": {"class": "secret_exposure", "kind": "guard", "severity": "MEDIUM", "tool": "semgrep",
                          "message": "A secret is compared with ==. Use hmac.compare_digest."},
        "silent-except": {"class": "silent_failure", "kind": "convention", "severity": "LOW", "tool": "ruff",
                          "message": "A bare except-pass hides failures."},
        "missing-timeout": {"class": "missing_timeout", "kind": "guard", "severity": "BOGUS", "tool": "ruff",
                            "message": "No timeout."},
    },
    "hits": [
        {"rule": "token-compare", "path": "app/auth.py", "line": 12, "text": "if t != TOKEN:", "detail": ""},
        {"rule": "silent-except", "path": "app/x.py", "line": 3, "text": "except Exception:", "detail": "S110"},
        {"rule": "missing-timeout", "path": "app/net.py", "line": 7, "text": "requests.get(u)", "detail": "S113"},
    ],
}
FIXTURES = [{"rule": r, "ok": r != "missing-timeout", "positive_hits": 1, "negative_hits": 0}
            for r in ("token-compare", "silent-except", "missing-timeout", "a", "b")]


def fake_catalogue(tmp_path: Path, *, fail: bool = False) -> Path:
    d = tmp_path / "suite-rules"
    d.mkdir()
    (d / "catalogue.toml").write_text("version = 1\n")
    (d / "check.py").write_text(textwrap.dedent(f"""
        import json, sys
        if {fail}:
            sys.exit("boom")
        args = sys.argv[1:]
        if args[0] == "scan":
            print({json.dumps(json.dumps(SCAN))})
        elif args[0] == "fixtures":
            print({json.dumps(json.dumps(FIXTURES))})
        open({str(tmp_path / 'args.json')!r}, "w").write(json.dumps(args))
    """))
    return d


def test_scan_maps_guard_hits_to_findings_and_skips_conventions(tmp_path: Path) -> None:
    cdir = fake_catalogue(tmp_path)
    out = suite_rules.scan("argus-lens", tmp_path / "repo", cdir=cdir)
    assert [f.id for f in out] == ["suite:token-compare:app/auth.py:12", "suite:missing-timeout:app/net.py:7"]
    tok = out[0]
    assert (tok.source, tok.type, tok.severity) == ("suite", "security", "MEDIUM")
    assert tok.file_path == str(tmp_path / "repo" / "app/auth.py") and tok.evidence == "if t != TOKEN:"
    assert tok.title.startswith("token-compare: A secret is compared with ==")
    assert out[1].type == "bug" and out[1].severity == "MEDIUM"  # unknown severity falls back
    # The class comes straight from the rule id.
    m = fc.classify(tok.title, source="suite", rule_id=fc.static_rule_id(tok.id, "suite"))
    assert (m.cls, m.rule) == ("secret_exposure", "static:suite:token-compare")


def test_scan_passes_paths_and_never_raises(tmp_path: Path) -> None:
    cdir = fake_catalogue(tmp_path)
    suite_rules.scan("r", tmp_path, ["a.py", "b/c.py"], cdir=cdir)
    assert json.loads((tmp_path / "args.json").read_text())[-3:] == ["--", "a.py", "b/c.py"]
    suite_rules.scan("r", tmp_path, ["-x.py"], cdir=cdir)   # a path, never an option such as --root
    assert json.loads((tmp_path / "args.json").read_text())[-2:] == ["--", "-x.py"]
    broken = tmp_path / "broken"
    broken.mkdir()
    assert suite_rules.scan("r", tmp_path, cdir=fake_catalogue(broken, fail=True)) == []
    assert suite_rules.scan("r", tmp_path) == []  # no catalogue (tests point at an empty dir)


def test_fixtures_feed_the_gate(tmp_path: Path) -> None:
    rep = suite_rules.fixtures(tmp_path, cdir=fake_catalogue(tmp_path))
    assert (rep["n"], rep["ok"], rep["rate"]) == (5, 4, 0.8)
    assert json.loads((tmp_path / suite_rules.FIXTURES_NAME).read_text())["n"] == 5
    g = {x.id: x for x in trust_eval.build_gates(None, None, None, None, rep)}["rules.fixtures"]
    assert g.status == "fail" and g.n == 5
    ok = {**rep, "ok": 5, "rate": 1.0}
    assert {x.id: x for x in trust_eval.build_gates(None, None, None, None, ok)}["rules.fixtures"].status == "pass"
    none = {x.id: x for x in trust_eval.build_gates(None, None, None, None, None)}["rules.fixtures"]
    assert none.status == "insufficient" and "catalogue" in none.note
    assert suite_rules.fixtures(tmp_path) is None


@pytest.mark.parametrize("printed,n,note", [
    ("null", 0, "unexpected fixtures output: NoneType"),
    ('{"error": "boom"}', 0, "unexpected fixtures output: dict"),
    ('[1, "x", {"rule": "a", "ok": true}]', 1, ""),
])
def test_fixtures_survive_output_of_the_wrong_shape(tmp_path: Path, printed: str, n: int, note: str) -> None:
    d = tmp_path / "suite-rules"
    d.mkdir()
    (d / "check.py").write_text(f"print({printed!r})\n")
    rep = suite_rules.fixtures(None, cdir=d)
    assert (rep["n"], rep["ok"], rep["note"]) == (n, n, note)


def test_taxonomy_maps_every_suite_guard_rule() -> None:
    tax = fc.taxonomy()
    assert set(tax.static_rules["suite"]) == {"missing-timeout", "predictable-tmp", "request-path-to-fs",
                                              "request-url-outbound", "token-compare", "agpl-import"}
    assert all(tax.get(c).guard != "none" for c in tax.static_rules["suite"].values())


def test_suite_packs_get_their_class_without_a_model(tmp_path: Path) -> None:
    d = packs_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "p1.yaml").write_text(json.dumps({
        "work_item_id": "p1", "kind": "nightly", "status": "open", "repo": {"name": "r"},
        "narrative": {"title": "token-compare: A secret is compared with ==", "description": "d"},
        "provenance": {"source": "suite", "finding_id": "suite:token-compare:app/auth.py:12"}}))
    rep = pack_classes.run(tmp_path, runner_factory=lambda: pytest.fail("no model call for a static rule"))
    assert rep["written"] == 1 and rep["classified_now"] == 0
    got = load_pack(tmp_path, "p1")["governance"]["finding_class"]
    assert got["id"] == "secret_exposure" and got["by"] == "static:suite"


def test_catalogue_is_read_from_dev_not_the_checkout(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    m = tmp_path / "argus-ci"
    m.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=m, check=True, capture_output=True)
    run("init", "-q", "-b", "dev")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (m / "suite-rules").mkdir()
    (m / "suite-rules" / "check.py").write_text("print('v1')\n")
    (m / "suite-rules" / "catalogue.toml").write_text("version = 1\n")
    run("add", ".")
    run("commit", "-qm", "rules")
    run("update-ref", "refs/remotes/origin/dev", "HEAD")   # the merged catalogue, as a mirror has it
    run("checkout", "-q", "-b", "work")               # a repair branch checked out, without the rules
    run("rm", "-rq", "suite-rules")
    run("commit", "-qm", "drop")
    cache = tmp_path / "cache"
    d = suite_rules.export_catalogue(m, cache)
    assert d is not None and (d / "check.py").read_text() == "print('v1')\n"   # HEAD has none; origin/dev does
    assert suite_rules.export_catalogue(m, cache) == d                          # cached by tree hash
    monkeypatch.delenv("AI_AUDIT_SUITE_RULES_DIR", raising=False)
    monkeypatch.setattr("auditor.trust_metrics._mirror_paths", lambda *_: {"argus-ci": m})
    got = suite_rules.catalogue_dir(cache_root=tmp_path / "out")          # cache_root is the output dir
    assert got == tmp_path / "out" / "_suite_rules" / d.parent.name / "suite-rules" and (got / "check.py").exists()


def test_export_content_matches_the_tree_it_is_cached_under(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    m = tmp_path / "argus-ci"
    m.mkdir()
    real_run = subprocess.run
    run = lambda *a: real_run(["git", *a], cwd=m, check=True, capture_output=True)
    run("init", "-q", "-b", "dev")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (m / "suite-rules").mkdir()
    (m / "suite-rules" / "check.py").write_text("print('v1')\n")
    run("add", ".")
    run("commit", "-qm", "v1")
    run("update-ref", "refs/remotes/origin/dev", "HEAD")
    v1_tree = run("rev-parse", "HEAD:suite-rules").stdout.decode().strip()

    def racing(cmd, *a, **kw):  # a fetch lands in the mirror right after the tree id is read
        out = real_run(cmd, *a, **kw)
        if "rev-parse" in cmd and not racing.moved:
            racing.moved = True
            (m / "suite-rules" / "check.py").write_text("print('v2')\n")
            run("commit", "-qam", "v2")
            run("update-ref", "refs/remotes/origin/dev", "HEAD")
        return out

    racing.moved = False
    monkeypatch.setattr(suite_rules.subprocess, "run", racing)
    d = suite_rules.export_catalogue(m, tmp_path / "cache")
    assert d is not None and d.parent.name == v1_tree[:16]
    assert (d / "check.py").read_text() == "print('v1')\n"
