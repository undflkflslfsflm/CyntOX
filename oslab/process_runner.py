from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from oslab.config import safe_subprocess_env
from oslab.policy import redact
from oslab.schemas import utc_now


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    timed_out: bool
    output_truncated: bool


class SafeProcessRunner:
    def __init__(self, output_limit: int = 2_000_000) -> None:
        self.output_limit = output_limit

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> ProcessResult:
        if not argv or any("\x00" in item for item in argv):
            raise ValueError("invalid argv")
        started = utc_now()
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=safe_subprocess_env(env),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
        except TimeoutError:
            timed_out = True
            await self._kill_tree(process)
            stdout, stderr = await process.communicate()
        ended = utc_now()
        combined_len = len(stdout) + len(stderr)
        truncated = combined_len > self.output_limit
        if truncated:
            half = self.output_limit // 2
            stdout = stdout[:half]
            stderr = stderr[:half]
        return ProcessResult(
            tuple(argv),
            process.returncode if process.returncode is not None else -1,
            redact(stdout.decode("utf-8", errors="replace")),
            redact(stderr.decode("utf-8", errors="replace")),
            started,
            ended,
            int((ended - started).total_seconds() * 1000),
            timed_out,
            truncated,
        )

    async def _kill_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            kill_process_group = cast(Callable[[int, int], None], os.__dict__.get("killpg"))
            if kill_process_group is None:
                raise RuntimeError("POSIX process-group termination is unavailable")
            with contextlib.suppress(ProcessLookupError):
                kill_process_group(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    kill_process_group(process.pid, cast(int, signal.__dict__.get("SIGKILL", 9)))
        with contextlib.suppress(ProcessLookupError):
            process.kill()
