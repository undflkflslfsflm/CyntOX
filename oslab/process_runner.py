from __future__ import annotations

import asyncio
import contextlib
import ctypes
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

WINDOWS_KILL_TREE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rootPid = [int]$args[0]
$processes = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId
$childrenByParent = @{}
foreach ($process in $processes) {
    $parent = [int]$process.ParentProcessId
    if (-not $childrenByParent.ContainsKey($parent)) {
        $childrenByParent[$parent] = New-Object System.Collections.ArrayList
    }
    [void]$childrenByParent[$parent].Add([int]$process.ProcessId)
}
$stack = New-Object System.Collections.Stack
$stack.Push($rootPid)
$ids = New-Object System.Collections.Generic.List[int]
while ($stack.Count -gt 0) {
    $pid = [int]$stack.Pop()
    $ids.Add($pid)
    if ($childrenByParent.ContainsKey($pid)) {
        foreach ($childPid in $childrenByParent[$pid]) {
            $stack.Push([int]$childPid)
        }
    }
}
$array = $ids.ToArray()
[array]::Reverse($array)
foreach ($pid in $array) {
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
}
"""


class _WindowsKillOnCloseJob:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows job objects are only available on Windows")

        from ctypes import wintypes

        class LargeInteger(ctypes.Structure):
            _fields_ = [("QuadPart", ctypes.c_longlong)]

        class JobObjectBasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", LargeInteger),
                ("PerJobUserTimeLimit", LargeInteger),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JobObjectExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JobObjectBasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._handle = self._kernel32.CreateJobObjectW(None, None)
        self._closed = False
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

        info = JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._kernel32.SetInformationJobObject(
            self._handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: asyncio.subprocess.Process) -> bool:
        process_handle = self._process_handle(process)
        if process_handle is None:
            return False
        ok = self._kernel32.AssignProcessToJobObject(
            self._handle, self._wintypes.HANDLE(process_handle)
        )
        return bool(ok)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    @staticmethod
    def _process_handle(process: asyncio.subprocess.Process) -> int | None:
        transport = getattr(process, "_transport", None)
        if transport is None:
            return None
        popen = transport.get_extra_info("subprocess")
        handle = getattr(popen, "_handle", None)
        return int(handle) if handle else None


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
        inherit_safe_env: bool = True,
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
            env=safe_subprocess_env(env) if inherit_safe_env else (env or {}),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        windows_job: _WindowsKillOnCloseJob | None = None
        if os.name == "nt":
            with contextlib.suppress(Exception):
                windows_job = _WindowsKillOnCloseJob()
                if not windows_job.assign(process):
                    windows_job.close()
                    windows_job = None
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
        except TimeoutError:
            timed_out = True
            await self._kill_tree(process, windows_job)
            stdout, stderr = await process.communicate()
        finally:
            if windows_job is not None:
                windows_job.close()
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

    async def _kill_tree(
        self, process: asyncio.subprocess.Process, windows_job: _WindowsKillOnCloseJob | None = None
    ) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            root_pid = process.pid
            if windows_job is not None:
                windows_job.close()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 2)
            await self._kill_windows_tree(root_pid)
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

    async def _kill_windows_tree(self, root_pid: int) -> None:
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                WINDOWS_KILL_TREE_SCRIPT,
                str(root_pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), 5)

        with contextlib.suppress(Exception):
            fallback = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(root_pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(fallback.wait(), 5)
