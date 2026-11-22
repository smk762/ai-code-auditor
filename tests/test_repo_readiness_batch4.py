"""scripts/repo_readiness.py: issue bodies and prechecks that don't crash or leak (batch-4 packs)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.test_repo_readiness_packs import _load

PRE = {"opsec": ["docs/setup.md:L4 private IP: 192.168.200.198", "README.md:L9 email: ops@corp.example.net"],
       "undocumented_env": {}, "undocumented_routes": {}, "undocumented_subcommands": {},
       "undocumented_entry_points": [], "package_init_without_all": [], "module_docstring_pct": None,
       "oversize_in_tree": [], "docs": ["README.md", "docs/setup.md"]}
GI = {"readme": "README.md", "readme_last_date": "2026-11-01", "readme_last_commit": "abc", "code_commits_since_readme": 3}


def test_a_public_issue_lists_opsec_hits_without_their_values(monkeypatch) -> None:
    mod = _load(monkeypatch)
    body = mod.issue_body("argus-x", "a" * 40, GI, PRE, None, public=True)
    assert "192.168.200.198" not in body and "ops@corp.example.net" not in body
    assert "`docs/setup.md:L4 private IP`" in body and "`README.md:L9 email`" in body
    private = mod.issue_body("argus-x", "a" * 40, GI, PRE, None, public=False)
    assert "192.168.200.198" in private  # a private repo's draft keeps the value to make the fix easy


def test_unknown_or_oddly_cased_severities_sort_last_instead_of_crashing(monkeypatch) -> None:
    mod = _load(monkeypatch)
    f = {"kind": "stale", "fix": "fix", "readme_says": "a", "code_shows": "b", "evidence": "x.py:L1"}
    rev = {"readme_freshness": 3, "summary": "ok", "findings": [
        f | {"severity": "info", "fix": "fourth"}, f | {"severity": "High", "fix": "first"},
        f | {"severity": "low", "fix": "third"}, f | {"severity": "medium", "fix": "second"}]}
    body = mod.issue_body("argus-x", "a" * 40, GI, PRE | {"opsec": []}, rev, public=False)
    assert [body.index(w) < body.index(n) for w, n in (("first", "second"), ("second", "third"), ("third", "fourth"))] \
        == [True, True, True]


@pytest.mark.parametrize("exc", [ValueError("source code string cannot contain null bytes"), RecursionError()])
def test_a_source_file_ast_cannot_parse_is_skipped_not_fatal(tmp_path: Path, monkeypatch, exc) -> None:
    mod = _load(monkeypatch)
    (tmp_path / "good.py").write_text('"""Doc."""\n')
    (tmp_path / "bad.py").write_bytes(b"x = 1\x00\n")
    real = ast.parse

    def parse(text, *a, **k):  # what Python 3.11 does with NUL bytes (3.12+ raises SyntaxError)
        if "\x00" in text:
            raise exc
        return real(text, *a, **k)

    monkeypatch.setattr(mod.ast, "parse", parse)
    pre = mod.prechecks(tmp_path)
    assert pre["module_docstring_pct"] == 100  # good.py counted, bad.py skipped
