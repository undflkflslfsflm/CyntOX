from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

import psutil

GPU_LEASE_DIR_ENV = "CYNTOX_GPU_LEASE_DIR"
GPU_UUID_ENV = "CYNTOX_GPU_UUID"
_PROCESS_CREATE_TIME_TOLERANCE_SECONDS = 0.01


def _finite_number(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _machine_lease_directory(base_dir: Path | None = None) -> Path:
    """Return one per-user machine lease directory, independent of a checkout."""
    if base_dir is not None:
        return base_dir.expanduser().resolve()
    override = os.environ.get(GPU_LEASE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "CyntOX" / "gpu-leases"
        return Path.home() / "AppData" / "Local" / "CyntOX" / "gpu-leases"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "cyntox" / "gpu-leases"
    return Path.home() / ".local" / "state" / "CyntOX" / "gpu-leases"


def _discover_gpu_uuid() -> str | None:
    override = os.environ.get(GPU_UUID_ENV, "").strip()
    if override:
        return override

    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",", 1)[0].strip()

    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            [
                executable,
                "--query-gpu=index,uuid",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    discovered: list[tuple[str, str]] = []
    for line in completed.stdout.splitlines():
        index, separator, gpu_uuid = line.partition(",")
        if separator and gpu_uuid.strip():
            discovered.append((index.strip(), gpu_uuid.strip()))
    if not discovered:
        return None
    if visible and visible not in {"-1", "none", "void"}:
        prefix_matches = [
            gpu_uuid
            for _index, gpu_uuid in discovered
            if gpu_uuid.casefold().startswith(visible.casefold())
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        for index, gpu_uuid in discovered:
            if index == visible:
                return gpu_uuid
        return None
    return discovered[0][1]


def selected_gpu_uuid() -> str | None:
    """Return the physical GPU identity used by the shared lease and runtime probes."""
    return _discover_gpu_uuid()


def default_gpu_lease_path(gpu_uuid: str | None = None, *, base_dir: Path | None = None) -> Path:
    """Return a safe per-user machine lease path for one physical GPU.

    When the physical UUID cannot be discovered, every unknown GPU maps to one
    conservative fallback lease instead of risking concurrent inference.
    """
    identity = (gpu_uuid or _discover_gpu_uuid() or "unknown-gpu").strip().casefold()
    readable = re.sub(r"[^a-z0-9]+", "-", identity).strip("-")[:48] or "unknown-gpu"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return _machine_lease_directory(base_dir) / f"{readable}-{digest}.lease"


class GpuLease:
    """Cross-process exclusive GPU lease with a process-bound owner heartbeat."""

    def __init__(self, path: Path, *, timeout: float = 60.0, heartbeat: float = 5.0) -> None:
        if timeout < 0:
            raise ValueError("GPU lease timeout must be non-negative")
        if heartbeat <= 0:
            raise ValueError("GPU lease heartbeat interval must be positive")
        self.path = path
        self.timeout = timeout
        self.heartbeat_interval = heartbeat
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._io_lock = threading.Lock()
        self._pid: int | None = None
        self._process_create_time: float | None = None
        self._nonce: str | None = None
        self._acquired_at: float | None = None
        self._lock_handle: BinaryIO | None = None
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")

    def _payload(self) -> dict[str, Any]:
        if (
            self._pid is None
            or self._process_create_time is None
            or self._nonce is None
            or self._acquired_at is None
        ):
            raise RuntimeError("GPU lease has no active owner identity")
        return {
            "version": 2,
            "pid": self._pid,
            "process_create_time": self._process_create_time,
            "nonce": self._nonce,
            "timestamp": self._acquired_at,
            "heartbeat": time.time(),
        }

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _owner_is_gone(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        pid = payload.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        except (psutil.Error, OverflowError, ValueError):
            return False

        expected_create_time = payload.get("process_create_time")
        if expected_create_time is not None:
            expected = _finite_number(expected_create_time)
            if expected is None:
                return False
            try:
                actual_create_time = process.create_time()
            except psutil.NoSuchProcess:
                return True
            except psutil.Error:
                return False
            if not math.isclose(
                actual_create_time,
                expected,
                rel_tol=0.0,
                abs_tol=_PROCESS_CREATE_TIME_TOLERANCE_SECONDS,
            ):
                return True
        try:
            return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            return False

    def _matches_owner(self, payload: object) -> bool:
        if not isinstance(payload, dict) or self._nonce is None or self._pid != os.getpid():
            return False
        pid = payload.get("pid")
        nonce = payload.get("nonce")
        create_time = payload.get("process_create_time")
        parsed_create_time = _finite_number(create_time)
        if (
            pid != self._pid
            or not isinstance(nonce, str)
            or parsed_create_time is None
            or self._process_create_time is None
        ):
            return False
        return hmac.compare_digest(nonce, self._nonce) and math.isclose(
            parsed_create_time,
            self._process_create_time,
            rel_tol=0.0,
            abs_tol=_PROCESS_CREATE_TIME_TOLERANCE_SECONDS,
        )

    def _try_kernel_lock(self) -> bool:
        handle = self._lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX  # type: ignore[attr-defined]
                    | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except OSError as error:
            handle.close()
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        self._lock_handle = handle
        return True

    def _release_kernel_lock(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
                )
        handle.close()

    def _write_payload_atomically(self) -> None:
        assert self._nonce is not None
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{self._nonce}.tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._payload(), handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def _atomic_heartbeat(self) -> bool:
        if not self._matches_owner(self._read_payload(self.path)):
            return False
        self._write_payload_atomically()
        return True

    def acquire(self) -> GpuLease:
        if self._nonce is not None:
            raise RuntimeError("GPU lease instance is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        self._pid = os.getpid()
        try:
            self._process_create_time = psutil.Process(self._pid).create_time()
        except psutil.Error as error:
            self._pid = None
            raise RuntimeError("could not determine GPU lease owner identity") from error
        self._nonce = secrets.token_hex(16)
        self._acquired_at = time.time()
        self._stop = threading.Event()
        try:
            while True:
                if self._try_kernel_lock():
                    observed = self._read_payload(self.path)
                    if (
                        isinstance(observed, dict)
                        and not self._matches_owner(observed)
                        and not self._owner_is_gone(observed)
                    ):
                        self._release_kernel_lock()
                    else:
                        self._write_payload_atomically()
                        break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"GPU lease remained busy: {self.path}")
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        except Exception:
            self._release_kernel_lock()
            self._clear_identity()
            raise
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"gpu-lease-heartbeat-{self._nonce[:8]}",
            daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            self.release()
            raise
        return self

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            try:
                with self._io_lock:
                    if not self._atomic_heartbeat():
                        return
            except OSError:
                continue

    def _clear_identity(self) -> None:
        self._pid = None
        self._process_create_time = None
        self._nonce = None
        self._acquired_at = None

    def release(self) -> None:
        if self._nonce is None:
            return
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, min(self.heartbeat_interval, 5.0) + 1.0))
        with self._io_lock:
            payload = self._read_payload(self.path)
            if self._matches_owner(payload):
                with contextlib.suppress(OSError):
                    self.path.unlink()
        self._release_kernel_lock()
        self._thread = None
        self._clear_identity()

    def __enter__(self) -> GpuLease:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
