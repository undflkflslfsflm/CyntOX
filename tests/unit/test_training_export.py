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
