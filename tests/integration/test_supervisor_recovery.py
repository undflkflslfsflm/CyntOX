import asyncio
from pathlib import Path

from oslab.campaign import prove_recovery
from oslab.config import default_config


def test_controlled_supervisor_crash_and_resume(tmp_path: Path) -> None:
    config = default_config(tmp_path)
    result = asyncio.run(prove_recovery(config))
    repeated = asyncio.run(prove_recovery(config))
    assert result["controlled_exit_code"] == 97
    assert result["final_state"] == "COMPLETE"
    assert result["finding_count"] == 1
    assert result["integrity"]["ok"]
    assert repeated["finding_count"] == 1
    assert repeated["integrity"]["ok"]
