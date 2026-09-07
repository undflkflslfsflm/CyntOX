# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from oslab.config import default_config
from oslab.fuzz import run_fixture_fuzz


@pytest.mark.qemu
def test_bounded_fixture_fuzz_checkpoint_minimize_and_coverage(tmp_path: Path) -> None:
    config = default_config(Path.cwd())
    config.runtime_root = tmp_path / "runtime"
    result = asyncio.run(
        run_fixture_fuzz(config, "pytest-fixture-fuzz", seed=101, total_iterations=6)
    )
    assert result["coverage"]["complete"]
    assert set(result["coverage"]["modes"]) == set(result["coverage"]["required_modes"])
    assert result["minimized"]["replay_outcome"] == "CRASH"
    assert result["minimized"]["minimized_bytes"] == 1
    assert Path(result["checkpoint"]).is_file()
