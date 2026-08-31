from pathlib import Path

from oslab.database import LabDatabase
from oslab.memory import MemoryIndex


def test_local_fts_returns_hashed_sources(tmp_path: Path) -> None:
    index = MemoryIndex(LabDatabase(tmp_path / "lab.sqlite3"))
    digest = index.add("kernel/mm.c:10-12", "allocator invariant panic", "abc123")
    hits = index.search("allocator")
    assert hits[0].source == "kernel/mm.c:10-12"
    assert hits[0].content_hash == digest
    assert hits[0].commit_id == "abc123"
