from pathlib import Path

import pytest

from oslab.config import default_config
from oslab.model import ModelRouter, ResourceScheduler, WorkloadKind


def test_model_router_profiles_are_evidence_bounded(tmp_path: Path) -> None:
    config = default_config(tmp_path)
    assert config.model.context_tokens == 32768
    assert config.model.output_tokens == 8192
    router = ModelRouter(config, {"ollama"})
    profiles = router.profiles()
    assert profiles["fast"].enabled
    assert profiles["fast"].context_tokens <= profiles["deep"].context_tokens
    assert profiles["oracle"].enabled is False
    assert "offloaded" in profiles["oracle"].reason


def test_model_router_rejects_disabled_profile(tmp_path: Path) -> None:
    config = default_config(tmp_path)
    router = ModelRouter(config, {"ollama"})
    with pytest.raises(RuntimeError):
        router.provider("oracle")


def test_resource_scheduler_defers_disk_heavy_offload() -> None:
    scheduler = ResourceScheduler()
    assert scheduler.can_run_together(WorkloadKind.MODEL, WorkloadKind.QEMU)
    assert not scheduler.can_run_together(WorkloadKind.DISK_HEAVY_OFFLOAD, WorkloadKind.FUZZ)
    assert scheduler.admission_reason(
        [WorkloadKind.QEMU], WorkloadKind.DISK_HEAVY_OFFLOAD
    ).startswith("defer")
