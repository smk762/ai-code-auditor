"""ecosystem_briefing: test suites and fixtures never feed the repo's endpoint list."""
from __future__ import annotations

from auditor import ecosystem_briefing as eb


def test_endpoints_from_tests_and_fixtures_are_left_out(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text('@app.get("/health")\n')
    for d in ("tests", "test", "fixtures", "app/tests"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / "helpers.py").write_text('@app.get("/admin")\nr = "GET /alpha"\n')
    (tmp_path / "app" / "test_api.py").write_text('@app.get("/a")\n')
    assert eb._discover_repo_endpoints(tmp_path) == ["GET /health"]
