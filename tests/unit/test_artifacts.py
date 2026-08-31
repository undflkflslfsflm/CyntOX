from pathlib import Path

import pytest

from oslab.artifacts import ArtifactStore


def test_content_addressed_storage_and_tamper_detection(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    first = store.put_bytes(b"evidence", "one")
    second = store.put_bytes(b"evidence", "two")
    assert first.sha256 == second.sha256
    assert store.get(first.sha256) == b"evidence"
    Path(first.blob_path).write_bytes(b"tampered")
    with pytest.raises(OSError, match="integrity"):
        store.get(first.sha256)
    assert not store.verify()["ok"]
