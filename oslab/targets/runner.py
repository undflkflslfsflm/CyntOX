from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

from oslab.artifacts import ArtifactStore
from oslab.policy import validate_qemu_network
from oslab.process_runner import ProcessResult, SafeProcessRunner
from oslab.qemu.backend import IMAGE, DockerQemuBackend
from oslab.qemu.qmp import QmpClient, QmpError
from oslab.schemas import Outcome, utc_now
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


def plan_manifest_qemu_args(loaded: LoadedTargetManifest, build_root: Path) -> list[str]:
    boot = loaded.manifest.boot
    if boot.qemu.accelerator not in {"auto", "tcg"}:
        raise ValueError("Docker-backed manifest QEMU smoke supports accelerator auto/tcg")
    machine = boot.qemu.machine
    if "accel=" in machine.lower():
        raise ValueError("QEMU accelerator must be declared with boot.qemu.accelerator")
    args = [
        "qemu-system-x86_64",
        "-machine",
        f"{machine},accel=tcg",
        "-cpu",
        boot.qemu.cpu,
        "-m",
        f"{boot.qemu.memory_mb}M",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "stdio",
        "-no-reboot",
        "-no-shutdown",
        "-snapshot",
        "-nic",
        "none",
    ]
    bootable = False
    initrd: str | None = None
    for artifact in boot.artifacts:
        path = _resolve_within(build_root, artifact.path)
        if not path.is_file():
            raise FileNotFoundError(path)
        container_path = _container_path(artifact.path)
        if artifact.kind == "kernel":
            args.extend(["-kernel", container_path])
            bootable = True
        elif artifact.kind == "initrd":
            initrd = container_path
        elif artifact.kind == "disk":
            args.extend(
                ["-drive", f"file={container_path},format={_qemu_image_format(path)},if=ide"]
            )
            bootable = True
        elif artifact.kind == "iso":
            args.extend(["-cdrom", container_path, "-boot", "d"])
            bootable = True
        elif artifact.kind == "firmware":
            args.extend(["-bios", container_path])
    if initrd is not None:
        args.extend(["-initrd", initrd])
    for device in boot.qemu.devices:
        args.extend(["-device", device])
    if not bootable:
        raise ValueError("manifest boot artifacts must include a kernel, disk, or iso")
    validate_qemu_network(args)
    return args


async def run_manifest_smoke(
    repo: Path,
    profile_name: str,
    test_id: str,
    runner: SafeProcessRunner,
    artifacts: ArtifactStore,
    *,
    worktrees_root: Path,
    lab_root: Path,
    timeout: float = 180.0,
) -> dict[str, Any]:
    loaded = load_target_manifest(repo)
    selected = next((test for test in loaded.manifest.tests if test.id == test_id), None)
    if selected is None:
        raise ValueError(f"unknown target test: {test_id}")
    if selected.kind != "smoke" or selected.transport != "serial":
        raise ValueError("manifest QEMU smoke currently supports serial smoke tests")

    build = await run_manifest_build(
        repo,
        profile_name,
        runner,
        artifacts,
        worktrees_root=worktrees_root,
        timeout=timeout,
    )
    if not build["ok"]:
        return {
            "target": loaded.manifest.name,
            "profile": profile_name,
            "test_id": test_id,
            "ok": False,
            "outcome": Outcome.BUILD_ERROR,
            "build": build,
        }

    build_worktree = Path(str(build["build_worktree"]))
    qemu_args = plan_manifest_qemu_args(loaded, build_worktree)
    result = await _run_manifest_qemu_smoke(
        loaded,
        qemu_args,
        build_worktree,
        selected.input,
        selected.success_patterns,
        artifacts,
        lab_root.resolve(),
        timeout=min(timeout, 120.0),
    )
    return {
        "target": loaded.manifest.name,
        "profile": profile_name,
        "test_id": test_id,
        "build": build,
        **result,
    }


async def _run_manifest_qemu_smoke(
    loaded: LoadedTargetManifest,
    qemu_args: list[str],
    build_worktree: Path,
    serial_input: str,
    success_patterns: list[str],
    artifacts: ArtifactStore,
    lab_root: Path,
    timeout: float,
) -> dict[str, Any]:
    await DockerQemuBackend(lab_root, artifacts).ensure_toolchain()
    docker = shutil.which("docker")
    if docker is None:
        raise FileNotFoundError("docker")
    port = _free_port()
    run_id = str(uuid4())
    container_name = f"oslab-real-{run_id[:8]}-{uuid4().hex[:6]}"
    qmp_args = [*qemu_args, "-qmp", "tcp:0.0.0.0:4444,server=on,wait=off"]
    validate_qemu_network(qmp_args)
    argv = [
        docker,
        "run",
        "--rm",
        "-i",
        "--name",
        container_name,
        "--mount",
        f"type=bind,source={build_worktree},target=/target",
        "--publish",
        f"127.0.0.1:{port}:4444",
        IMAGE,
        *qmp_args,
    ]
    started = utc_now()
    serial = bytearray()
    stderr = bytearray()
    qmp: QmpClient | None = None
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=lab_root,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=0x00000200 if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    serial_task = asyncio.create_task(_drain(process.stdout, serial))
    stderr_task = asyncio.create_task(_drain(process.stderr, stderr))
    outcome = Outcome.INFRA_ERROR
    ok = False
    phase = "boot"
    try:
        qmp = QmpClient("127.0.0.1", port)
        await qmp.connect(min(timeout, 15.0))
        for pattern in loaded.manifest.boot.readiness_patterns:
            await _wait_for_serial(serial, process, pattern, min(timeout, 30.0))
        phase = "test"
        if serial_input and process.stdin is not None:
            process.stdin.write(serial_input.encode())
            await process.stdin.drain()
        for pattern in success_patterns:
            await _wait_for_serial(serial, process, pattern, min(timeout, 30.0))
        ok = True
        outcome = Outcome.PASS
    except TimeoutError as exc:
        outcome = Outcome.HANG if phase == "test" else Outcome.BOOT_ERROR
        stderr.extend(f"\n{type(exc).__name__}: {exc}\n".encode())
    except (OSError, QmpError, ValueError) as exc:
        outcome = Outcome.INFRA_ERROR
        stderr.extend(f"\n{type(exc).__name__}: {exc}\n".encode())
    finally:
        if qmp is not None:
            with contextlib.suppress(QmpError, TimeoutError, OSError):
                await qmp.execute("quit", timeout=3)
            with contextlib.suppress(QmpError, OSError):
                await qmp.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            await _kill_docker_container(docker, container_name)
            process.kill()
            await process.wait()
        await serial_task
        await stderr_task

    ended = utc_now()
    serial_text = serial.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    serial_record = artifacts.put_bytes(
        serial_text.encode(), f"manifest-qemu-{run_id}-serial.log", "text/plain"
    )
    stderr_record = artifacts.put_bytes(
        stderr_text.encode(), f"manifest-qemu-{run_id}-stderr.log", "text/plain"
    )
    qemu_argv_hash = hashlib.sha256(" ".join(qemu_args).encode()).hexdigest()
    return {
        "ok": ok,
        "outcome": outcome,
        "run_id": run_id,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_ms": int((ended - started).total_seconds() * 1000),
        "build_worktree": str(build_worktree),
        "qemu_argv_hash": qemu_argv_hash,
        "network": "none",
        "readiness_patterns": loaded.manifest.boot.readiness_patterns,
        "success_patterns": success_patterns,
        "serial_excerpt": serial_text[-4000:],
        "stderr_excerpt": stderr_text[-4000:],
        "artifacts": {"serial": serial_record.sha256, "stderr": stderr_record.sha256},
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


async def _wait_for_serial(
    serial: bytearray, process: asyncio.subprocess.Process, pattern: str, timeout: float
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pattern in serial.decode("utf-8", errors="replace"):
            return
        if process.returncode is not None:
            raise OSError(f"QEMU exited before serial pattern {pattern!r}")
        await asyncio.sleep(0.02)
    raise TimeoutError(f"serial pattern not observed: {pattern}")


async def _drain(stream: asyncio.StreamReader | None, target: bytearray) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        target.extend(chunk)
        if len(target) > 2_000_000:
            del target[0 : len(target) - 2_000_000]


async def _kill_docker_container(docker: str, name: str) -> None:
    process = await asyncio.create_subprocess_exec(
        docker,
        "kill",
        name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()


def _container_path(relative_path: str) -> str:
    return "/target/" + relative_path.replace("\\", "/")


def _qemu_image_format(path: Path) -> str:
    return "qcow2" if path.suffix.lower() == ".qcow2" else "raw"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _resolve_within(root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escapes target source root")
    return resolved
