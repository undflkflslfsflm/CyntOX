from pathlib import Path

from oslab.config import default_config
from oslab.targets import inspect_targets


def test_target_registry_reports_fixture_and_precise_real_os_blocker(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "boot"
    fixture.mkdir(parents=True)
    (fixture / "boot.asm").write_text("bits 16\n", encoding="utf-8")
    result = inspect_targets(default_config(tmp_path))
    assert result["fixture"]["status"] == "ready"
    assert result["real_os"]["status"] == "absent"
    assert result["gate_l"] == "blocked_missing_external_input"
    assert "AUTHORIZED_OS_SOURCE_PATH" in result["resume_command"]
