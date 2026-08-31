from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from oslab.schemas import utc_now

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, config_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, state TEXT NOT NULL,
      manifest_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(experiment_id) REFERENCES experiments(id));
    CREATE TABLE IF NOT EXISTS state_transitions(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
      from_state TEXT, to_state TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(run_id) REFERENCES runs(id));
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
      kind TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(run_id) REFERENCES runs(id));
    CREATE TABLE IF NOT EXISTS artifacts(sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, logical_name TEXT NOT NULL,
      media_type TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS model_invocations(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, model_id TEXT NOT NULL,
      prompt_hash TEXT NOT NULL, response_hash TEXT, settings_json TEXT NOT NULL, usage_json TEXT NOT NULL,
      outcome TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
      outcome TEXT NOT NULL, reproducer_hash TEXT, patch_hash TEXT, verification_json TEXT NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(fingerprint, patch_hash));
    CREATE TABLE IF NOT EXISTS tool_calls(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool TEXT NOT NULL,
      request_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, entity_type TEXT NOT NULL,
      payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(source, content, commit_id, content_hash, tokenize='unicode61');
    CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
    CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type, run_id);
    """,
)


class LabDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> int:
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, script in enumerate(MIGRATIONS, start=1):
                if version in current:
                    continue
                connection.executescript(script)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now().isoformat()),
                )
        return len(MIGRATIONS)

    def record_entity(self, entity_id: str, run_id: str, entity_type: str, payload: Any) -> None:
        encoded = json.dumps(payload, sort_keys=True, default=str)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO entities(id, run_id, entity_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (entity_id, run_id, entity_type, encoded, utc_now().isoformat()),
            )

    def integrity_check(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
            foreign = [
                dict(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
            ]
            migrations = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        return {
            "integrity": rows,
            "foreign_key_errors": foreign,
            "migrations": migrations,
            "ok": rows == ["ok"] and not foreign,
        }

    def backup(self, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target

    def restore(self, source: Path) -> None:
        if not source.is_file():
            raise FileNotFoundError(source)
        probe = sqlite3.connect(source)
        try:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise OSError("backup database failed integrity check")
        finally:
            probe.close()
        shutil.copy2(source, self.path)
