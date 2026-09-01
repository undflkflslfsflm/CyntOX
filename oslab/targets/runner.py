from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from oslab.artifacts import ArtifactStore
from oslab.process_runner import ProcessResult, SafeProcessRunner
from oslab.targets.manifest import LoadedTargetManifest, load_target_manifest


async def list_manifest_build_profiles(repo: Path) -> dict[str, Any]:
    loaded = load_target_manifest(repo)
    return {
        "target": loaded.manifest.name,
        "source_root": str(loaded.source_root),
        "base_commit": loaded.manifest.source.base_commit,
        "git": loaded.git,
        "profiles": [
            {
                "profile": name,
                "description": profile.description,
                "commands": len(profile.commands),
                "artifacts": [artifact.model_dump(mode="json") for artifact in profile.artifacts],
                "env_allowlist": profile.env_allowlist,
            }
            for name, profile in sorted(loaded.manifest.build.profiles.items())
        ],
    }


async def run_manifest_build(
    repo: Path,
    profile_name: str,
    runner: SafeProcessRunner,
    artifacts: ArtifactStore,
    *,
    worktrees_root: Path,
    timeout: float = 180.0,
) -> dict[str, Any]:
    loaded = load_target_manifest(repo)
    profile = loaded.manifest.build.profiles.get(profile_name)
    if profile is None:
        raise ValueError(f"unknown build profile: {profile_name}")

    build_worktree = await _create_build_worktree(loaded, runner, worktrees_root)
    worktree_head = (
        await _git(runner, build_worktree, "rev-parse", "HEAD", timeout=min(timeout, 60))
    ).stdout.strip()
    pre_build_status = (
        await _git(runner, build_worktree, "status", "--short", timeout=min(timeout, 60))
    ).stdout.strip()
    env = {name: os.environ[name] for name in profile.env_allowlist if name in os.environ}
    command_rows: list[dict[str, Any]] = []
    ok = True
    for index, command in enumerate(profile.commands):
        cwd = _resolve_within(build_worktree, command.cwd)
        result = await runner.run(
            list(command.argv),
            cwd=cwd,
            timeout=timeout,
            env=env,
            inherit_safe_env=False,
        )
        row = {
            "index": index,
            "argv": list(result.argv),
            "cwd": str(cwd),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
        }
        command_rows.append(row)
        if result.returncode != 0 or result.timed_out:
            ok = False
            break

    artifact_rows: list[dict[str, Any]] = []
    missing_artifacts: list[dict[str, str]] = []
    if ok:
        for artifact in profile.artifacts:
            path = _resolve_within(build_worktree, artifact.path)
            if not path.is_file():
                ok = False
                missing_artifacts.append({"path": artifact.path, "kind": artifact.kind})
                continue
            record = artifacts.put_file(path, f"{loaded.manifest.name}-{profile_name}-{path.name}")
            artifact_rows.append(
                {
                    "path": str(path),
                    "relative_path": artifact.path,
                    "kind": artifact.kind,
                    "sha256": record.sha256,
                    "size": record.size,
                    "media_type": record.media_type,
                }
            )
    post_build_status = (
        await _git(runner, build_worktree, "status", "--short", timeout=min(timeout, 60))
    ).stdout.strip()

    return {
        "target": loaded.manifest.name,
        "profile": profile_name,
        "source_root": str(loaded.source_root),
        "build_worktree": str(build_worktree),
        "base_commit": loaded.manifest.source.base_commit,
        "worktree_head": worktree_head,
        "pre_build_status": pre_build_status,
        "post_build_status": post_build_status,
        "git": loaded.git,
        "ok": ok,
        "commands": command_rows,
        "artifacts": artifact_rows,
        "missing_artifacts": missing_artifacts,
    }


async def _create_build_worktree(
    loaded: LoadedTargetManifest, runner: SafeProcessRunner, worktrees_root: Path
) -> Path:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    worktrees_root.mkdir(parents=True, exist_ok=True)
    target = (
        worktrees_root
        / f"{loaded.manifest.name}-{loaded.manifest.source.base_commit[:12]}-{uuid4().hex[:8]}"
    ).resolve()
    if target.exists():
        raise FileExistsError(target)
    result = await runner.run(
        [
            git,
            "worktree",
            "add",
            "--detach",
            str(target),
            loaded.manifest.source.base_commit,
        ],
        cwd=loaded.source_root,
        timeout=60,
    )
    if result.returncode != 0:
        raise OSError(result.stderr or result.stdout)
    return target


async def _git(runner: SafeProcessRunner, cwd: Path, *args: str, timeout: float) -> ProcessResult:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    result = await runner.run([git, *args], cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        raise OSError(result.stderr or result.stdout)
    return result


def _resolve_within(root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escapes target source root")
    return resolved
