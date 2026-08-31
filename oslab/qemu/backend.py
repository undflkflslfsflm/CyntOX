from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from oslab.artifacts import ArtifactStore
from oslab.policy import validate_qemu_network
from oslab.qemu.qmp import QmpClient, QmpError
from oslab.schemas import Outcome, utc_now

IMAGE = "qwen-os-lab-qemu:bookworm"


@dataclass(frozen=True)
class FixtureResult:
    run_id: str
    outcome: Outcome
    started_at: str
    ended_at: str
    serial_log: str
    events: tuple[dict[str, Any], ...]
    artifacts: dict[str, str]
    details: dict[str, Any] = field(default_factory=dict)


class DockerQemuBackend:
    def __init__(self, project_root: Path, artifacts: ArtifactStore) -> None:
        self.project_root = project_root.resolve()
        self.artifacts = artifacts
        self.runtime = self.project_root / ".oslab" / "qemu"
        self.runtime.mkdir(parents=True, exist_ok=True)
        docker = shutil.which("docker")
        if not docker:
            raise FileNotFoundError("docker")
        self.docker: str = docker
        self.process: asyncio.subprocess.Process | None = None
        self.qmp: QmpClient | None = None
        self.serial = bytearray()
        self.stderr = bytearray()
        self._serial_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._container_name: str | None = None
        self._run_id: str | None = None
        self._event_context: dict[str, Any] = {}

    async def ensure_toolchain(self) -> dict[str, Any]:
        inspect = await self._command([self.docker, "image", "inspect", IMAGE], 30)
        if inspect[0] != 0:
            code, stdout, stderr = await self._command(
                [
                    self.docker,
                    "build",
                    "--tag",
                    IMAGE,
                    "--file",
                    str(self.project_root / "fixtures" / "boot" / "Dockerfile"),
                    str(self.project_root),
                ],
                600,
            )
            if code != 0:
                raise OSError(stderr or stdout)
        code, stdout, stderr = await self._command(
            [
                self.docker,
                "run",
                "--rm",
                IMAGE,
                "sh",
                "-c",
                "qemu-system-x86_64 --version | head -1; nasm -v; gdb --version | head -1; dpkg-query -W qemu-system-x86 nasm gdb",
            ],
            60,
        )
        if code != 0:
            raise OSError(stderr or stdout)
        return {"image": IMAGE, "versions": stdout}

    async def build_fixture(self) -> dict[str, Any]:
        await self.ensure_toolchain()
        code, stdout, stderr = await self._command(
            [
                self.docker,
                "run",
                "--rm",
                "--mount",
                self._mount(),
                IMAGE,
                "sh",
                "/lab/fixtures/boot/build.sh",
            ],
            60,
        )
        if code != 0:
            raise OSError(stderr or stdout)
        fixture = self.project_root / ".oslab" / "fixture"
        records = {
            name: self.artifacts.put_file(fixture / name, f"fixture-{name}").sha256
            for name in ("boot-sector.bin", "base.raw", "overlay.qcow2")
        }
        return {"stdout": stdout, "hashes": records}

    async def boot(
        self, *, run_id: str | None = None, seed: int = 1, test_id: str = "boot"
    ) -> dict[str, Any]:
        if self.process is not None:
            raise RuntimeError("VM is already running")
        await self.build_fixture()
        self._run_id = run_id or str(uuid4())
        self._event_context = {
            "run_id": self._run_id,
            "seed": seed,
            "test_id": test_id,
            "build_id": hashlib.sha256(
                (self.project_root / ".oslab" / "fixture" / "boot-sector.bin").read_bytes()
            ).hexdigest(),
        }
        port = self._free_port()
        self._container_name = f"oslab-qemu-{self._run_id[:8]}-{uuid4().hex[:6]}"
        qemu_args = [
            "qemu-system-x86_64",
            "-machine",
            "pc,accel=tcg",
            "-cpu",
            "max",
            "-m",
            "64M",
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-no-reboot",
            "-no-shutdown",
            "-nic",
            "none",
            "-qmp",
            "tcp:0.0.0.0:4444,server=on,wait=off",
            "-drive",
            "file=/lab/.oslab/fixture/overlay.qcow2,format=qcow2,if=ide",
        ]
        validate_qemu_network(qemu_args)
        argv = [
            self.docker,
            "run",
            "--rm",
            "-i",
            "--name",
            self._container_name,
            "--mount",
            self._mount(),
            "--publish",
            f"127.0.0.1:{port}:4444",
            IMAGE,
            *qemu_args,
        ]
        self.serial.clear()
        self.stderr.clear()
        creationflags = 0x00000200 if os.name == "nt" else 0
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.project_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        self._serial_task = asyncio.create_task(self._drain(self.process.stdout, self.serial))
        self._stderr_task = asyncio.create_task(self._drain(self.process.stderr, self.stderr))
        self.qmp = QmpClient("127.0.0.1", port)
        await self.qmp.connect(15)
        ready = await self.wait_for('"event":"READY"', 15)
        return {"run_id": self._run_id, "qmp_port": port, "ready": ready, **self._event_context}

    async def send(self, command: str) -> None:
        if len(command) != 1 or command not in "PFCHIQB":
            raise ValueError("invalid fixture console command")
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("VM is not running")
        self.process.stdin.write(command.encode())
        await self.process.stdin.drain()

    async def wait_for(self, pattern: str, timeout: float, occurrence: int = 1) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            text = self.serial.decode("utf-8", errors="replace")
            if text.count(pattern) >= occurrence:
                return text
            if self.process is not None and self.process.returncode is not None:
                raise OSError(f"QEMU exited before pattern {pattern!r}: {self._stderr_text()}")
            await asyncio.sleep(0.02)
        raise TimeoutError(f"serial pattern not observed: {pattern}")

    async def save_snapshot(self, name: str) -> str:
        if self.qmp is None:
            raise RuntimeError("QMP is not connected")
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid snapshot name")
        result = await self.qmp.hmp(f"savevm {name}", 30)
        if "error" in result.lower():
            raise QmpError(result)
        return result

    async def restore_snapshot(self, name: str) -> str:
        if self.qmp is None:
            raise RuntimeError("QMP is not connected")
        result = await self.qmp.hmp(f"loadvm {name}", 30)
        if "error" in result.lower():
            raise QmpError(result)
        return result

    async def power_cycle(self) -> None:
        if self.qmp is None:
            raise RuntimeError("QMP is not connected")
        await self.qmp.execute("system_reset")

    async def stop(self) -> None:
        if self.process is None:
            return
        if self.qmp is not None:
            with contextlib.suppress(QmpError, TimeoutError):
                await self.qmp.execute("quit", timeout=3)
            await self.qmp.close()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            if self._container_name:
                await self._command([self.docker, "kill", self._container_name], 10)
            self.process.kill()
            await self.process.wait()
        for task in (self._serial_task, self._stderr_task):
            if task is not None:
                await task
        self.process = None
        self.qmp = None
        self._serial_task = None
        self._stderr_task = None

    async def exercise(self, mode: str, *, seed: int = 1) -> FixtureResult:
        started = utc_now()
        run_id = str(uuid4())
        outcome = Outcome.INFRA_ERROR
        details: dict[str, Any] = {"mode": mode, "accelerator": "tcg", "network": "none"}
        try:
            await self.boot(run_id=run_id, seed=seed, test_id=mode)
            if mode == "pass":
                await self.send("P")
                await self.wait_for('"event":"PASS"', 5)
                outcome = Outcome.PASS
            elif mode == "fail":
                await self.send("F")
                await self.wait_for('"event":"FAIL"', 5)
                outcome = Outcome.FAIL
            elif mode == "crash":
                await self.send("C")
                await self.wait_for('"event":"CRASH"', 5)
                outcome = Outcome.CRASH
            elif mode == "hang":
                await self.send("H")
                await self.wait_for('"event":"HANG_ARMED"', 5)
                try:
                    await self.wait_for('"event":"PASS"', 0.5)
                    outcome = Outcome.FAIL
                except TimeoutError:
                    outcome = Outcome.HANG
            elif mode == "snapshot":
                await self.send("I")
                await self.wait_for("OSLAB_STATE 1", 5)
                await self.save_snapshot("proof")
                await self.send("I")
                await self.wait_for("OSLAB_STATE 2", 5, 1)
                await self.restore_snapshot("proof")
                before = self.serial.decode("utf-8", errors="replace").count("OSLAB_STATE 2")
                await self.send("I")
                await self.wait_for("OSLAB_STATE 2", 5, before + 1)
                details["snapshot_state_replayed"] = True
                outcome = Outcome.PASS
            elif mode == "seeded":
                await self.send("B")
                await self.wait_for("OSLAB_BUG_VALUE", 5)
                serial = self.serial.decode("utf-8", errors="replace")
                outcome = Outcome.PASS if "OSLAB_BUG_VALUE 4" in serial else Outcome.FAIL
            else:
                raise ValueError(f"unknown fixture mode: {mode}")
        except (OSError, QmpError, TimeoutError, ValueError) as exc:
            details["error"] = f"{type(exc).__name__}: {exc}"
            if outcome not in {Outcome.HANG, Outcome.CRASH, Outcome.FAIL}:
                outcome = Outcome.INFRA_ERROR
        finally:
            await self.stop()
        ended = utc_now()
        serial_text = self.serial.decode("utf-8", errors="replace")
        events = tuple(self._parse_events(serial_text, started, ended))
        serial_record = self.artifacts.put_bytes(
            serial_text.encode(), f"qemu-{run_id}-serial.log", "text/plain"
        )
        stderr_record = self.artifacts.put_bytes(
            self._stderr_text().encode(), f"qemu-{run_id}-stderr.log", "text/plain"
        )
        return FixtureResult(
            run_id,
            outcome,
            started.isoformat(),
            ended.isoformat(),
            serial_text,
            events,
            {"serial": serial_record.sha256, "stderr": stderr_record.sha256},
            details,
        )

    def _parse_events(self, serial: str, started: Any, ended: Any) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        duration_ms = int((ended - started).total_seconds() * 1000)
        for line in serial.splitlines():
            if not line.startswith("OSLAB_EVT "):
                continue
            try:
                event = json.loads(line.removeprefix("OSLAB_EVT "))
            except json.JSONDecodeError:
                continue
            event.update(self._event_context)
            event["duration_ms"] = duration_ms
            values.append(event)
        return values

    async def _command(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.project_root,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError as exc:
            process.kill()
            stdout, stderr = await process.communicate()
            raise TimeoutError(f"command timed out: {argv[1]}") from exc
        return (
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def _drain(self, stream: asyncio.StreamReader | None, target: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            target.extend(chunk)
            if len(target) > 2_000_000:
                del target[0 : len(target) - 2_000_000]

    def _mount(self) -> str:
        return f"type=bind,source={self.project_root},target=/lab"

    def _stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
