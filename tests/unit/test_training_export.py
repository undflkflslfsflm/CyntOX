# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
import hashlib
from pathlib import Path

import pyarrow.parquet as pq

from oslab.schemas import TrajectoryEvent
from oslab.training import export_trajectories


def test_verified_trajectory_jsonl_parquet_dry_run(tmp_path: Path) -> None:
    verified = TrajectoryEvent(
        trajectory_id="t1",
        sequence=0,
        kind="verification",
        role="verifier",
        content={"defect_family": "bounds", "license": "Apache-2.0"},
        evidence_hashes=["a" * 64],
        verified=True,
    )
    unverified = verified.model_copy(update={"trajectory_id": "t2", "verified": False})
    result = export_trajectories([verified, unverified], tmp_path)
    assert result["records"] == 1
    assert pq.read_table(result["parquet"]).num_rows == 1


def test_trajectory_export_is_byte_stable(tmp_path: Path) -> None:
    event = TrajectoryEvent(
        trajectory_id="stable",
        sequence=0,
        kind="verification",
        role="verifier",
        content={"defect_family": "bounds", "license": "Apache-2.0"},
        evidence_hashes=["b" * 64],
        verified=True,
    )
    first = export_trajectories([event], tmp_path / "first")
    second = export_trajectories([event], tmp_path / "second")

    for key in ("jsonl", "parquet", "card"):
        left = hashlib.sha256(Path(first[key]).read_bytes()).hexdigest()
        right = hashlib.sha256(Path(second[key]).read_bytes()).hexdigest()
        assert left == right
    assert b"\r\n" not in Path(first["card"]).read_bytes()
