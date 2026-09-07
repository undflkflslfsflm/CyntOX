# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from oslab.artifacts import ArtifactStore
from oslab.config import default_config
from oslab.process_runner import SafeProcessRunner
from oslab.targets import (
    inspect_target_manifest,
    inspect_targets,
    load_target_manifest,
    manifest_template_json,
    plan_manifest_qemu_args,
)
from oslab.targets.runner import run_manifest_build

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


def _marked_buildable_git_target(root: Path) -> str:
    (root / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (root / "kernel").mkdir()
    (root / "kernel" / ".keep").write_text("", encoding="utf-8")
    (root / "boot").mkdir()
    (root / "boot" / ".keep").write_text("", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "build_target.py").write_text(
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                "Path('build').mkdir(exist_ok=True)",
                "Path('build/kernel.bin').write_bytes(b'kernel')",
                "Path('build/env.txt').write_text(str('USERPROFILE' in os.environ), encoding='utf-8')",
                "print('real-target-build-ok')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(root, "init")
    _git(root, "config", "user.email", "oslab@example.invalid")
    _git(root, "config", "user.name", "OS Lab Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "seed buildable target")
    return _git(root, "rev-parse", "HEAD")


def _manifest_for_python_build(base_commit: str) -> str:
    return (
        manifest_template_json()["content"]
        .replace("<immutable git commit sha>", base_commit)
        .replace(
            'argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/build.ps1", "-Configuration", "Debug"]',
            f'argv = [{json.dumps(sys.executable)}, "scripts/build_target.py"]',
        )
    )


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


def test_manifest_build_runs_in_disposable_worktree_and_hashes_artifacts(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    base_commit = _marked_buildable_git_target(repo)
    (repo / "oslab-target.toml").write_text(
        _manifest_for_python_build(base_commit), encoding="utf-8"
    )
    monkeypatch.setenv("USERPROFILE", "should-not-leak")  # type: ignore[attr-defined]

    result = asyncio.run(
        run_manifest_build(
            repo,
            "debug",
            SafeProcessRunner(),
            ArtifactStore(tmp_path / "artifacts"),
            worktrees_root=tmp_path / "runtime" / "worktrees",
            timeout=30,
        )
    )

    build_worktree = Path(result["build_worktree"])
    assert result["ok"] is True
    assert result["worktree_head"] == base_commit
    assert result["commands"][0]["exit_code"] == 0
    assert (
        result["artifacts"][0]["sha256"]
        == "6923dd1bc0460082c5d55a831908c24a282860b7f1cd6c2b79cf1bc8857c639c"
    )
    assert not (repo / "build").exists()
    assert (build_worktree / "build" / "kernel.bin").read_bytes() == b"kernel"
    assert (build_worktree / "build" / "env.txt").read_text(encoding="utf-8") == "False"


def test_manifest_build_reports_missing_declared_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    base_commit = _marked_buildable_git_target(repo)
    manifest = _manifest_for_python_build(base_commit).replace(
        'path = "build/kernel.bin"', 'path = "build/missing.bin"', 1
    )
    (repo / "oslab-target.toml").write_text(manifest, encoding="utf-8")

    result = asyncio.run(
        run_manifest_build(
            repo,
            "debug",
            SafeProcessRunner(),
            ArtifactStore(tmp_path / "artifacts"),
            worktrees_root=tmp_path / "runtime" / "worktrees",
            timeout=30,
        )
    )

    assert result["ok"] is False
    assert result["missing_artifacts"] == [{"path": "build/missing.bin", "kind": "kernel"}]


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


def test_target_manifest_rejects_serial_smoke_without_success_pattern(tmp_path: Path) -> None:
    base_commit = _marked_git_target(tmp_path)
    bad = (
        manifest_template_json()["content"]
        .replace("<immutable git commit sha>", base_commit)
        .replace('success_patterns = ["OSLAB_SMOKE_PASS"]\n', "")
    )
    (tmp_path / "oslab-target.toml").write_text(bad, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)

    assert manifest["status"] == "invalid"
    assert "success_patterns" in str(manifest["errors"])


def test_target_manifest_rejects_qemu_network_devices(tmp_path: Path) -> None:
    base_commit = _marked_git_target(tmp_path)
    bad = (
        manifest_template_json()["content"]
        .replace("<immutable git commit sha>", base_commit)
        .replace("devices = []", 'devices = ["e1000"]')
    )
    (tmp_path / "oslab-target.toml").write_text(bad, encoding="utf-8")

    manifest = inspect_target_manifest(tmp_path)

    assert manifest["status"] == "invalid"
    assert "network devices are forbidden" in str(manifest["errors"])


def test_manifest_qemu_plan_uses_declared_disk_and_no_network(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    base_commit = _marked_buildable_git_target(repo)
    manifest = (
        _manifest_for_python_build(base_commit)
        .replace('path = "build/kernel.bin"', 'path = "build/disk.raw"')
        .replace('kind = "kernel"', 'kind = "disk"')
    )
    (repo / "oslab-target.toml").write_text(manifest, encoding="utf-8")
    build_root = tmp_path / "build-root"
    (build_root / "build").mkdir(parents=True)
    (build_root / "build" / "disk.raw").write_bytes(b"boot")

    args = plan_manifest_qemu_args(load_target_manifest(repo), build_root)

    assert "-nic" in args
    assert "none" in args
    assert "-drive" in args
    assert any(item == "file=/target/build/disk.raw,format=raw,if=ide" for item in args)


def test_tracked_manifest_example_matches_cli_template() -> None:
    example = Path("config/oslab-target.example.toml").read_text(encoding="utf-8")

    assert example == manifest_template_json()["content"]
