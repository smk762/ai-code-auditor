from __future__ import annotations

import sqlite3
from pathlib import Path

from auditor.contracts import CodeUnit


class GraphBuilder:
    def __init__(self, db_path: str = "graph/ecosystem_graph.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                repo TEXT NOT NULL,
                file_path TEXT,
                metadata TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                UNIQUE(source_id, target_id, edge_type)
            )
            """
        )
        self.conn.commit()

    def upsert_code_units(self, units: list[CodeUnit]) -> None:
        cur = self.conn.cursor()
        for unit in units:
            cur.execute(
                """
                INSERT INTO nodes (id, kind, name, repo, file_path, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    name=excluded.name,
                    repo=excluded.repo,
                    file_path=excluded.file_path,
                    metadata=excluded.metadata
                """,
                (unit.id, unit.kind.upper(), unit.symbol, unit.repo, unit.file_path, str(unit.ast_features)),
            )
            for imp in unit.ast_features.get("imports", []):
                import_id = f"import::{unit.repo}::{imp}"
                cur.execute(
                    """
                    INSERT INTO nodes (id, kind, name, repo, file_path, metadata)
                    VALUES (?, 'MODULE', ?, ?, '', '')
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (import_id, imp, unit.repo),
                )
                cur.execute(
                    """
                    INSERT OR IGNORE INTO edges (source_id, target_id, edge_type)
                    VALUES (?, ?, 'IMPORTS')
                    """,
                    (unit.id, import_id),
                )
        self.conn.commit()

    def add_dependency(self, source_repo: str, target_repo: str) -> None:
        cur = self.conn.cursor()
        src_id = f"repo::{source_repo}"
        tgt_id = f"repo::{target_repo}"
        cur.execute(
            "INSERT INTO nodes (id, kind, name, repo, file_path, metadata) VALUES (?, 'REPOSITORY', ?, ?, '', '') ON CONFLICT(id) DO NOTHING",
            (src_id, source_repo, source_repo),
        )
        cur.execute(
            "INSERT INTO nodes (id, kind, name, repo, file_path, metadata) VALUES (?, 'REPOSITORY', ?, ?, '', '') ON CONFLICT(id) DO NOTHING",
            (tgt_id, target_repo, target_repo),
        )
        cur.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, edge_type) VALUES (?, ?, 'DEPENDS_ON')",
            (src_id, tgt_id),
        )
        self.conn.commit()

    def query_dependencies(self, repo_name: str) -> list[str]:
        cur = self.conn.cursor()
        src_id = f"repo::{repo_name}"
        rows = cur.execute(
            """
            SELECT n.name
            FROM edges e
            JOIN nodes n ON n.id = e.target_id
            WHERE e.source_id = ? AND e.edge_type = 'DEPENDS_ON'
            """,
            (src_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def query_callers(self, repo_name: str) -> list[str]:
        """Return names of repos that declare a DEPENDS_ON edge targeting *repo_name*."""
        cur = self.conn.cursor()
        tgt_id = f"repo::{repo_name}"
        rows = cur.execute(
            """
            SELECT n.name
            FROM edges e
            JOIN nodes n ON n.id = e.source_id
            WHERE e.target_id = ? AND e.edge_type = 'DEPENDS_ON'
            """,
            (tgt_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def all_edges(self) -> list[tuple[str, str, str]]:
        cur = self.conn.cursor()
        return cur.execute("SELECT source_id, target_id, edge_type FROM edges").fetchall()

    def close(self) -> None:
        self.conn.close()
