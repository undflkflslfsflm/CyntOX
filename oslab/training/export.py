from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from oslab.schemas import TrajectoryEvent


def _split_key(event: TrajectoryEvent) -> str:
    family = str(event.content.get("defect_family", event.trajectory_id))
    bucket = int(hashlib.sha256(family.encode()).hexdigest()[:8], 16) % 10
    if bucket < 7:
        return "train"
    if bucket < 9:
        return "validation"
    return "test"


def export_trajectories(events: list[TrajectoryEvent], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    verified = [event for event in events if event.verified]
    deduplicated: dict[str, TrajectoryEvent] = {}
    for event in verified:
        digest = hashlib.sha256(event.model_dump_json().encode()).hexdigest()
        deduplicated.setdefault(digest, event)
    rows = [
        {
            **event.model_dump(mode="json"),
            "split": _split_key(event),
            "content_json": json.dumps(event.content, sort_keys=True),
        }
        for event in deduplicated.values()
    ]
    jsonl = output / "trajectories.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    parquet = output / "trajectories.parquet"
    pq.write_table(pa.Table.from_pylist(rows), parquet)
    card = output / "DATASET_CARD.md"
    card.write_text(
        "# Qwen OS Lab Verified Trajectories\n\n"
        f"Records: {len(rows)}. Only verifier-approved, evidence-linked events are exported. "
        "Splits are assigned by defect family to reduce leakage. Source licenses and model/runtime "
        "identities remain attached to each trajectory.\n",
        encoding="utf-8",
    )
    return {"records": len(rows), "jsonl": str(jsonl), "parquet": str(parquet), "card": str(card)}
