"""Regression tests for auditor/ecosystem_briefing.py budget, discovery and parsing."""
from __future__ import annotations

from pathlib import Path

from auditor import ecosystem_briefing as eb


def _many_roles(n: int = 30) -> dict[str, dict[str, str]]:
    return {
        f"argus-repo{i:02d}": {"label": f"Area {i}", "role": "Does a fairly long list of things " * 3}
        for i in range(n)
    }


def test_overview_keeps_footer_and_whole_lines(monkeypatch):
    roles = _many_roles()
    monkeypatch.setattr(eb, "_get_repo_roles", lambda: roles)
    out = eb.build_ecosystem_overview(max_chars=2000)
    assert len(out) <= 2000
    assert out.endswith("argus-cortex owns taxonomy/wire schemas; do not reimplement in demo UIs.")
    assert "Pipeline: quarry" in out
    lines = out.splitlines()
    repo_lines = [ln for ln in lines if ln.startswith("- **")]
    # every kept repo line is whole
    full = {f"- **{r}** ({i['label']}): {i['role']}" for r, i in roles.items()}
    assert repo_lines and all(ln in full for ln in repo_lines)
    assert f"- … and {len(roles) - len(repo_lines)} more repos" in lines


def test_overview_unchanged_when_everything_fits(monkeypatch):
    roles = _many_roles(2)
    monkeypatch.setattr(eb, "_get_repo_roles", lambda: roles)
    out = eb.build_ecosystem_overview(max_chars=8000)
    assert "more repos" not in out
    assert sum(ln.startswith("- **") for ln in out.splitlines()) == 2


class _Graph:
    def query_dependencies(self, name):
        return [f"dep-{i:03d}" for i in range(400)]

    def query_callers(self, name):
        return ["caller-a"]


def test_repo_briefing_keeps_security_notes(monkeypatch, tmp_path):
    monkeypatch.setattr(eb, "_get_repo_roles", lambda: {"agent-composer": {"label": "RAG", "role": "chat"}})
    out = eb.build_repo_briefing("agent-composer", tmp_path, graph=_Graph(), max_chars=1200)
    assert len(out) <= 1200
    assert "Security context:" in out
    for note in eb._SECURITY_NOTES["agent-composer"]:
        assert note in out
    assert "… truncated" in out


def test_endpoints_skip_env_dirs_dedupe_and_sort(monkeypatch, tmp_path):
    monkeypatch.setattr(eb, "_get_repo_roles", lambda: {})
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "b.py").write_text('@app.get("/zeta")\n@app.get("/alpha")\n')
    (tmp_path / "app" / "a.py").write_text('@router.get("/alpha")\n')
    for env in ("venv", "env", ".tox", "build", "dist", "site-packages"):
        d = tmp_path / env / "lib"
        d.mkdir(parents=True)
        (d / "x.py").write_text(f'@app.post("/from-{env}")\n')
    out = eb.build_repo_briefing("x", tmp_path, max_chars=4000)
    line = next(ln for ln in out.splitlines() if ln.startswith("Exposes endpoints:"))
    assert line == "Exposes endpoints: GET /alpha, GET /zeta"
    assert "more" not in out


def test_load_repo_roles_matches_heading_as_whole_line(monkeypatch, tmp_path):
    md = tmp_path / "ecosystem.md"
    md.write_text(
        "# Eco\n\n"
        "## Repository layout\n\n"
        "| Area | Repo | Role |\n|---|---|---|\n| Wrong | [wrong-repo](u) | not this |\n\n"
        "## Repos\n\n"
        "| Area | Repo | Role |\n|---|---|---|\n| Taxonomy | [argus-cortex](u) | foundation |\n\n"
        "## Other\n\n"
        "| Area | Repo | Role |\n|---|---|---|\n| Late | [late-repo](u) | after |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(eb, "_ECOSYSTEM_MD", md)
    roles = eb._load_repo_roles()
    assert roles == {"argus-cortex": {"label": "Taxonomy", "role": "foundation"}}
