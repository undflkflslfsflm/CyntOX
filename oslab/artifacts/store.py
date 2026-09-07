from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactRecord:
    sha256: str
    size: int
    media_type: str
    blob_path: str
    logical_name: str


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.blobs = self.root / "blobs" / "sha256"
        self.index = self.root / "artifact-index.jsonl"
        self.blobs.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def put_bytes(
        self, data: bytes, logical_name: str, media_type: str = "application/octet-stream"
    ) -> ArtifactRecord:
        digest = self.digest(data)
        target = self.blobs / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            descriptor, temp_name = tempfile.mkstemp(prefix="artifact-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        record = ArtifactRecord(digest, len(data), media_type, str(target), logical_name)
        self._append_index(record)
        return record

    def put_json(self, value: Any, logical_name: str) -> ArtifactRecord:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        return self.put_bytes(data, logical_name, "application/json")

    def put_file(self, source: Path, logical_name: str | None = None) -> ArtifactRecord:
        data = source.read_bytes()
        return self.put_bytes(data, logical_name or source.name)

    def get(self, sha256: str) -> bytes:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError("invalid SHA-256")
        path = self.blobs / sha256[:2] / sha256
        data = path.read_bytes()
        if self.digest(data) != sha256:
            raise OSError("artifact integrity mismatch")
        return data

    def verify(self, exclude_logical_names: set[str] | None = None) -> dict[str, Any]:
        excluded_names = exclude_logical_names or set()
        excluded_hashes = self._hashes_for_logical_names(excluded_names)
        checked = 0
        excluded = 0
        failures: list[str] = []
        for path in self.blobs.glob("*/*"):
            if not path.is_file():
                continue
            if path.name in excluded_hashes:
                excluded += 1
                continue
            checked += 1
            if self.digest(path.read_bytes()) != path.name:
                failures.append(str(path))
        return {"checked": checked, "excluded": excluded, "failures": failures, "ok": not failures}

    def _append_index(self, record: ArtifactRecord) -> None:
        self.index.parent.mkdir(parents=True, exist_ok=True)
        with self.index.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    def _hashes_for_logical_names(self, logical_names: set[str]) -> set[str]:
        if not logical_names or not self.index.is_file():
            return set()
        hashes: set[str] = set()
        for line in self.index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("logical_name") in logical_names and isinstance(row.get("sha256"), str):
                hashes.add(row["sha256"])
        return hashes
