from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from auditor import bootstrap_db


class _Conn:
    def __init__(self, executed: list[str]):
        self.dialect = postgresql.dialect()
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def scalar(self, *a, **kw):
        return None

    def execute(self, stmt, *a, **kw):
        self.executed.append(str(stmt))

    def exec_driver_sql(self, sql, *a, **kw):
        self.executed.append(sql)


def test_create_database_escapes_quotes_and_colons(monkeypatch: pytest.MonkeyPatch):
    executed: list[str] = []
    engine = SimpleNamespace(connect=lambda: _Conn(executed), dispose=lambda: None)
    monkeypatch.setattr(bootstrap_db, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        bootstrap_db, "get_settings",
        lambda: SimpleNamespace(database_url='postgresql://u:p@h:5432/we"ird:name'),
    )
    bootstrap_db.ensure_database_exists()
    assert executed == ['CREATE DATABASE "we""ird:name"']
