from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

ADDRESS = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,16}\b")
PID_TID = re.compile(r"\b(?:pid|tid)[=: ]+\d+\b", re.IGNORECASE)
TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-Z]+\b")


@dataclass(frozen=True)
class CrashEvidence:
    kind: str
    subsystem: str
    assertion: str | None
    frames: tuple[str, ...]
    build_lineage: str
    reproducer_hash: str


def normalize_log(value: str) -> str:
    value = TIMESTAMP.sub("<time>", value)
    value = PID_TID.sub("pid=<id>", value)
    value = ADDRESS.sub("<addr>", value)
    lines = [" ".join(line.split()) for line in value.splitlines() if line.strip()]
    return "\n".join(lines)


def fingerprint_crash(evidence: CrashEvidence) -> str:
    payload = {
        "kind": evidence.kind.upper(),
        "subsystem": evidence.subsystem.lower(),
        "assertion": normalize_log(evidence.assertion or ""),
        "frames": [normalize_log(frame) for frame in evidence.frames[:8]],
        "build_lineage": evidence.build_lineage,
        "reproducer_hash": evidence.reproducer_hash,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
