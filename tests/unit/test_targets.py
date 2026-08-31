from pathlib import Path

from oslab.config import default_config
from oslab.targets import inspect_target_manifest, inspect_targets, manifest_template_json


def test_target_registry_reports_fixture_and_precise_real_os_blocker(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "boot"
    fixture.mkdir(parents=True)
    (fixture / "boot.asm").write_text("bits 16\n", encoding="utf-8")
    result = inspect_targets(default_config(tmp_path))
    assert result["fixture"]["status"] == "ready"
    assert result["real_os"]["status"] == "absent"
    assert result["gate_l"] == "blocked_missing_external_input"
    assert "AUTHORIZED_OS_SOURCE_PATH" in result["resume_command"]


def test_target_manifest_template_is_valid_for_marked_os_candidate(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (tmp_path / "kernel").mkdir()
    (tmp_path / "boot").mkdir()
    template = manifest_template_json()["content"].replace(
        "<immutable git commit sha>", "abcdef1234567890"
    )
    (tmp_path / "oslab-target.toml").write_text(template, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)
    inspected = inspect_targets(default_config(tmp_path), tmp_path)

    assert manifest["status"] == "ready"
    assert manifest["qemu"]["network"] == "none"
    assert manifest["smoke_tests"] == ["smoke"]
    assert inspected["real_os"]["status"] == "ready"
    assert inspected["real_os"]["integration_status"] == "declarative-manifest-valid"
    assert inspected["gate_l"] == "applicable"


def test_target_manifest_rejects_public_network_and_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (tmp_path / "kernel").mkdir()
    (tmp_path / "boot").mkdir()
    bad = (
        manifest_template_json()["content"]
        .replace("<immutable git commit sha>", "abcdef1234567890")
        .replace('network = "none"', 'network = "user"')
        .replace('path = "build/kernel.bin"', 'path = "../escape.bin"', 1)
    )
    (tmp_path / "oslab-target.toml").write_text(bad, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)
    inspected = inspect_targets(default_config(tmp_path), tmp_path)

    assert manifest["status"] == "invalid"
    assert inspected["real_os"]["status"] == "invalid_manifest"
    assert inspected["gate_l"] == "blocked_invalid_or_incomplete_target"


def test_marked_os_candidate_requires_manifest_before_ready(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (tmp_path / "kernel").mkdir()
    (tmp_path / "boot").mkdir()

    inspected = inspect_targets(default_config(tmp_path), tmp_path)

    assert inspected["real_os"]["status"] == "candidate_manifest_required"
    assert inspected["gate_l"] == "blocked_invalid_or_incomplete_target"
