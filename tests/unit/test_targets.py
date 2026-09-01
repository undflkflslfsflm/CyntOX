import shutil
import subprocess
from pathlib import Path

from oslab.config import default_config
from oslab.targets import inspect_target_manifest, inspect_targets, manifest_template_json

FULL_SHA = "abcdef1234567890abcdef1234567890abcdef12"


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    completed = subprocess.run(  # noqa: S603 - resolved git binary with fixed argv, no shell
        [git, *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _marked_git_target(root: Path) -> str:
    (root / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (root / "kernel").mkdir()
    (root / "boot").mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "oslab@example.invalid")
    _git(root, "config", "user.name", "OS Lab Test")
    _git(root, "add", "Makefile", "kernel", "boot")
    _git(root, "commit", "-m", "seed target")
    return _git(root, "rev-parse", "HEAD")


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
    base_commit = _marked_git_target(tmp_path)
    template = manifest_template_json()["content"].replace(
        "<immutable git commit sha>", base_commit
    )
    (tmp_path / "oslab-target.toml").write_text(template, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)
    inspected = inspect_targets(default_config(tmp_path), tmp_path)

    assert manifest["status"] == "ready"
    assert manifest["git"]["base_commit_verified"] == base_commit
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
        .replace("<immutable git commit sha>", FULL_SHA)
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


def test_target_manifest_rejects_unknown_base_commit(tmp_path: Path) -> None:
    known_commit = _marked_git_target(tmp_path)
    unknown_commit = "f" * len(known_commit)
    template = manifest_template_json()["content"].replace(
        "<immutable git commit sha>", unknown_commit
    )
    (tmp_path / "oslab-target.toml").write_text(template, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)

    assert manifest["status"] == "invalid"
    assert "source.base_commit" in str(manifest["errors"])
    assert "does not resolve" in str(manifest["errors"])


def test_target_manifest_rejects_non_git_source_root(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (tmp_path / "kernel").mkdir()
    (tmp_path / "boot").mkdir()
    template = manifest_template_json()["content"].replace("<immutable git commit sha>", FULL_SHA)
    (tmp_path / "oslab-target.toml").write_text(template, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)

    assert manifest["status"] == "invalid"
    assert "Git repository root" in str(manifest["errors"])


def test_target_manifest_rejects_non_immutable_base_commit(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (tmp_path / "kernel").mkdir()
    (tmp_path / "boot").mkdir()
    bad = manifest_template_json()["content"].replace("<immutable git commit sha>", "main")
    (tmp_path / "oslab-target.toml").write_text(bad, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)

    assert manifest["status"] == "invalid"
    assert "base_commit" in str(manifest["errors"])


def test_target_manifest_rejects_shell_eval_commands(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (tmp_path / "kernel").mkdir()
    (tmp_path / "boot").mkdir()
    bad = (
        manifest_template_json()["content"]
        .replace("<immutable git commit sha>", FULL_SHA)
        .replace(
            'argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/build.ps1", "-Configuration", "Debug"]',
            'argv = ["powershell.exe", "-Command", "Invoke-Build"]',
        )
    )
    (tmp_path / "oslab-target.toml").write_text(bad, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)

    assert manifest["status"] == "invalid"
    assert "shell eval" in str(manifest["errors"])


def test_tracked_manifest_example_matches_cli_template() -> None:
    example = Path("config/oslab-target.example.toml").read_text(encoding="utf-8")

    assert example == manifest_template_json()["content"]
