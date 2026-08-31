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


def test_integrity_can_exclude_report_artifacts_by_logical_name(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    report = store.put_bytes(b"report", "latest-report.json")
    evidence = store.put_bytes(b"evidence", "evidence.json")

    result = store.verify(exclude_logical_names={"latest-report.json"})

    assert result["ok"]
    assert result["checked"] == 1
    assert result["excluded"] == 1
    assert store.get(evidence.sha256) == b"evidence"
    assert store.get(report.sha256) == b"report"
