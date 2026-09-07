from __future__ import annotations

import hashlib
from dataclasses import dataclass

from oslab.database import LabDatabase


@dataclass(frozen=True)
class MemoryHit:
    source: str
    content: str
    commit_id: str
    content_hash: str
    score: float


class MemoryIndex:
    def __init__(self, database: LabDatabase) -> None:
        self.database = database
        self.database.migrate()

    def add(self, source: str, content: str, commit_id: str) -> str:
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_fts(source, content, commit_id, content_hash) VALUES (?, ?, ?, ?)",
                (source, content, commit_id, digest),
            )
        return digest

    def search(self, query: str, limit: int = 10) -> list[MemoryHit]:
        if not query.strip():
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT source, content, commit_id, content_hash, bm25(memory_fts) AS rank "
                "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
        return [
            MemoryHit(
                row["source"],
                row["content"],
                row["commit_id"],
                row["content_hash"],
                -float(row["rank"]),
            )
            for row in rows
        ]
