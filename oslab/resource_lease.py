from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, BinaryIO

import psutil

RESOURCE_KINDS = ("qemu", "build", "fuzz")
AIRLLM_ADMISSION_KIND = "airllm"
_IO_RETRY_TIMEOUT_SECONDS = 0.25
_IO_RETRY_INITIAL_DELAY_SECONDS = 0.001
_IO_RETRY_MAX_DELAY_SECONDS = 0.02
_LOCK_RETRY_TIMEOUT_SECONDS = 1.0
_METADATA_LOCKS_GUARD = threading.Lock()
_METADATA_LOCKS: dict[Path, threading.RLock] = {}
_HELD_METADATA_LOCKS = threading.local()


class _ReadState(Enum):
    OK = auto()
    MISSING = auto()
    MALFORMED = auto()
    UNREADABLE = auto()


@dataclass(frozen=True)
class _ReadResult:
    state: _ReadState
    payload: dict[str, Any] | None = None


class ResourceLeaseConflictError(RuntimeError):
    """A cooperating resource lease is live, so the requested lease cannot start."""

    def __init__(
        self,
        requested_kind: str,
        active_kind: str,
        *,
        uncertain: bool = False,
    ) -> None:
        qualifier = "possibly active" if uncertain else "active"
        super().__init__(
            f"{requested_kind} resource lease conflicts with {qualifier} {active_kind} lease"
        )
        self.requested_kind = requested_kind
        self.active_kind = active_kind
        self.uncertain = uncertain


def resource_lease_path(root: Path, kind: str) -> Path:
    """Return the compatibility marker consumed by the council scheduler."""
    if kind not in RESOURCE_KINDS:
        raise ValueError(f"unknown resource activity kind: {kind}")
    return root.resolve() / ".oslab" / "resource-leases" / f"{kind}.json"


def airllm_admission_path(root: Path) -> Path:
    """Return the repository-wide AirLLM admission marker and lock key."""
    return root.resolve() / ".oslab" / "resource-leases" / "airllm-admission.json"


def _airllm_admission_path_for_resource(path: Path) -> Path:
    return path.parent / "airllm-admission.json"


def _local_metadata_lock(path: Path) -> tuple[Path, threading.RLock]:
    key = path.resolve()
    with _METADATA_LOCKS_GUARD:
        return key, _METADATA_LOCKS.setdefault(key, threading.RLock())


def _lock_metadata_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(  # type: ignore[attr-defined]
        handle.fileno(),
        fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
    )


def _unlock_metadata_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


@contextlib.contextmanager
def _metadata_guard(path: Path) -> Any:
    """Serialize one resource kind within and across cooperating processes."""
    key, local_lock = _local_metadata_lock(path)
    with local_lock:
        held: set[Path] = getattr(_HELD_METADATA_LOCKS, "paths", set())
        if key in held:
            yield
            return

        lock_path = key.with_name(f".{key.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + _LOCK_RETRY_TIMEOUT_SECONDS
            delay = _IO_RETRY_INITIAL_DELAY_SECONDS
            while True:
                try:
                    _lock_metadata_handle(handle)
                    break
                except OSError as error:
                    if not _retryable_io_error(error) or time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"resource metadata lock remained busy: {key}"
                        ) from error
                    time.sleep(delay)
                    delay = min(delay * 2, _IO_RETRY_MAX_DELAY_SECONDS)
            held.add(key)
            _HELD_METADATA_LOCKS.paths = held
            try:
                yield
            finally:
                held.remove(key)
                with contextlib.suppress(OSError):
                    _unlock_metadata_handle(handle)
        finally:
            handle.close()


def _atomic_json(path: Path, payload: dict[str, Any], *, lock_key: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        with _metadata_guard(lock_key or path):
            deadline = time.monotonic() + _IO_RETRY_TIMEOUT_SECONDS
            delay = _IO_RETRY_INITIAL_DELAY_SECONDS
            while True:
                try:
                    os.replace(temporary, path)
                    break
                except OSError as error:
                    if not _retryable_io_error(error) or time.monotonic() >= deadline:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2, _IO_RETRY_MAX_DELAY_SECONDS)
    finally:
        _unlink_raw_with_retries(Path(temporary))


def _retryable_io_error(error: OSError) -> bool:
    """Return whether an I/O error can be transient during atomic publication."""
    return error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or (
        os.name == "nt" and getattr(error, "winerror", None) in {5, 32, 33}
    )


def _unlink_raw_with_retries(path: Path) -> bool:
    deadline = time.monotonic() + _IO_RETRY_TIMEOUT_SECONDS
    delay = _IO_RETRY_INITIAL_DELAY_SECONDS
    while True:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as error:
            if not _retryable_io_error(error) or time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, _IO_RETRY_MAX_DELAY_SECONDS)


def _unlink_with_retries(path: Path, *, lock_key: Path | None = None) -> bool:
    try:
        with _metadata_guard(lock_key or path):
            return _unlink_raw_with_retries(path)
    except OSError:
        return False


def _read_payload_result(path: Path, *, lock_key: Path | None = None) -> _ReadResult:
    if not path.parent.is_dir():
        return _ReadResult(_ReadState.MISSING)
    deadline = time.monotonic() + _IO_RETRY_TIMEOUT_SECONDS
    delay = _IO_RETRY_INITIAL_DELAY_SECONDS
    while True:
        try:
            with _metadata_guard(lock_key or path):
                payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _ReadResult(_ReadState.MISSING)
        except OSError as error:
            if not _retryable_io_error(error) or time.monotonic() >= deadline:
                return _ReadResult(_ReadState.UNREADABLE)
            time.sleep(delay)
            delay = min(delay * 2, _IO_RETRY_MAX_DELAY_SECONDS)
            continue
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _ReadResult(_ReadState.MALFORMED)
        if not isinstance(payload, dict):
            return _ReadResult(_ReadState.MALFORMED)
        return _ReadResult(_ReadState.OK, payload)


def _read_payload(path: Path, *, lock_key: Path | None = None) -> dict[str, Any] | None:
    return _read_payload_result(path, lock_key=lock_key).payload


def read_resource_lease_payload(path: Path) -> dict[str, Any] | None:
    """Read a marker while tolerating transient atomic-replace sharing conflicts."""
    return _read_payload(path)


def _owner_liveness(payload: object) -> bool | None:
    if not isinstance(payload, dict):
        return False
    pid = payload.get("pid")
    created = payload.get("process_create_time")
    if not isinstance(pid, int) or not isinstance(created, int | float):
        return False
    try:
        actual = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError):
        return None
    return abs(actual - float(created)) < 0.01


def _owner_is_live(payload: object) -> bool:
    return _owner_liveness(payload) is True


def _heartbeat_value(payload: dict[str, Any]) -> float:
    heartbeat = payload.get("heartbeat")
    return float(heartbeat) if isinstance(heartbeat, int | float) else 0.0


def _owner_path_from_payload(path: Path, kind: str, payload: object) -> Path | None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    pid = payload.get("pid")
    nonce = payload.get("nonce")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(nonce, str)
        or len(nonce) != 32
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        return None
    return path.parent / ".owners" / kind / f"{pid}-{nonce}.json"


def _release_tombstone_path(owner_path: Path) -> Path:
    return owner_path.with_name(f"{owner_path.name}.released")


def _owner_is_released(path: Path, owner_path: Path, payload: object) -> bool:
    return _owner_release_state(path, owner_path, payload) is True


def _owner_release_state(path: Path, owner_path: Path, payload: object) -> bool | None:
    if not isinstance(payload, dict):
        return False
    nonce = payload.get("nonce")
    if not isinstance(nonce, str):
        return False
    result = _read_payload_result(_release_tombstone_path(owner_path), lock_key=path)
    if result.state is _ReadState.UNREADABLE:
        return None
    return (
        isinstance(result.payload, dict)
        and result.payload.get("nonce") == nonce
        and result.payload.get("released") is True
    )


def _marker_has_live_owner(path: Path, kind: str, payload: object) -> bool:
    owner_path = _owner_path_from_payload(path, kind, payload)
    if owner_path is None or _owner_is_released(path, owner_path, payload):
        return False
    result = _read_payload_result(owner_path, lock_key=path)
    if result.state is _ReadState.UNREADABLE:
        return _owner_is_live(payload)
    owner = result.payload
    if not isinstance(payload, dict) or not isinstance(owner, dict):
        return False
    return (
        owner.get("kind") == kind
        and owner.get("pid") == payload.get("pid")
        and owner.get("process_create_time") == payload.get("process_create_time")
        and owner.get("nonce") == payload.get("nonce")
        and _owner_is_live(owner)
    )


def _live_owner_payloads(path: Path, kind: str) -> list[dict[str, Any]]:
    with _metadata_guard(path):
        owner_dir = path.parent / ".owners" / kind
        candidates: list[dict[str, Any]] = []
        for owner_path in owner_dir.glob("*.json"):
            result = _read_payload_result(owner_path, lock_key=path)
            payload = result.payload
            if _owner_is_released(path, owner_path, payload):
                if _unlink_with_retries(owner_path, lock_key=path):
                    _unlink_with_retries(_release_tombstone_path(owner_path), lock_key=path)
                continue
            liveness = _owner_liveness(payload)
            if isinstance(payload, dict) and payload.get("kind") == kind and liveness is True:
                candidates.append(payload)
            elif result.state in {_ReadState.OK, _ReadState.MALFORMED} and liveness is not None:
                _unlink_with_retries(owner_path, lock_key=path)
        return candidates


def _promote_live_owner(path: Path, kind: str) -> dict[str, Any] | None:
    with _metadata_guard(path):
        candidates = _live_owner_payloads(path, kind)
        if not candidates:
            return None
        replacement = max(candidates, key=_heartbeat_value)
        with contextlib.suppress(OSError):
            _atomic_json(path, replacement, lock_key=path)
        return replacement


def _resource_kind_liveness_for_admission(path: Path, kind: str) -> bool | None:
    """Return live/stale/uncertain while the AirLLM gate prevents new publishers.

    Cross-process lock order is always the AirLLM admission marker first, followed
    by one normal resource-kind marker. Code holding a kind marker must never take
    the admission marker.
    """
    with _metadata_guard(path):
        marker_result = _read_payload_result(path, lock_key=path)
        if marker_result.state is _ReadState.UNREADABLE:
            return None

        owner_dir = path.parent / ".owners" / kind
        try:
            owner_paths = list(owner_dir.glob("*.json"))
        except OSError:
            return None

        candidates: list[dict[str, Any]] = []
        for owner_path in owner_paths:
            result = _read_payload_result(owner_path, lock_key=path)
            if result.state is _ReadState.UNREADABLE:
                return None
            payload = result.payload
            released = _owner_release_state(path, owner_path, payload)
            if released is None:
                return None
            if released:
                if _unlink_with_retries(owner_path, lock_key=path):
                    _unlink_with_retries(_release_tombstone_path(owner_path), lock_key=path)
                continue

            expected_owner_path = _owner_path_from_payload(path, kind, payload)
            if (
                isinstance(payload, dict)
                and payload.get("kind") == kind
                and expected_owner_path == owner_path
            ):
                liveness = _owner_liveness(payload)
                if liveness is True:
                    candidates.append(payload)
                    continue
                if liveness is None:
                    return None
            if result.state in {_ReadState.OK, _ReadState.MALFORMED}:
                _unlink_with_retries(owner_path, lock_key=path)

        if candidates:
            replacement = max(candidates, key=_heartbeat_value)
            with contextlib.suppress(OSError):
                _atomic_json(path, replacement, lock_key=path)
            return True

        if marker_result.state is not _ReadState.MISSING:
            _unlink_with_retries(path, lock_key=path)
        return False


def _first_resource_conflict(root: Path) -> tuple[str, bool] | None:
    """Return (kind, uncertain) for the first activity blocking AirLLM admission."""
    for kind in RESOURCE_KINDS:
        liveness = _resource_kind_liveness_for_admission(resource_lease_path(root, kind), kind)
        if liveness is not False:
            return kind, liveness is None
    return None


def _valid_airllm_admission_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    nonce = payload.get("nonce")
    return bool(
        payload.get("schema_version") == 1
        and payload.get("kind") == AIRLLM_ADMISSION_KIND
        and isinstance(payload.get("pid"), int)
        and not isinstance(payload.get("pid"), bool)
        and int(payload["pid"]) > 0
        and isinstance(payload.get("process_create_time"), int | float)
        and not isinstance(payload.get("process_create_time"), bool)
        and isinstance(nonce, str)
        and len(nonce) == 32
        and all(character in "0123456789abcdef" for character in nonce)
    )


def _airllm_admission_liveness(path: Path) -> bool | None:
    """Return whether an admission is live; None means it cannot be read safely."""
    with _metadata_guard(path):
        result = _read_payload_result(path, lock_key=path)
        if result.state is _ReadState.MISSING:
            _unlink_with_retries(_release_tombstone_path(path), lock_key=path)
            return False
        if result.state is _ReadState.UNREADABLE:
            return None

        payload = result.payload
        if not _valid_airllm_admission_payload(payload):
            removed = _unlink_with_retries(path, lock_key=path)
            if removed:
                _unlink_with_retries(_release_tombstone_path(path), lock_key=path)
                return False
            return None

        released = _owner_release_state(path, path, payload)
        if released is None:
            return None
        if released:
            removed = _unlink_with_retries(path, lock_key=path)
            if removed:
                _unlink_with_retries(_release_tombstone_path(path), lock_key=path)
            return False

        liveness = _owner_liveness(payload)
        if liveness is False:
            if _unlink_with_retries(path, lock_key=path):
                _unlink_with_retries(_release_tombstone_path(path), lock_key=path)
            return False
        return liveness


@dataclass
class _ActivityState:
    path: Path
    owner_path: Path
    kind: str
    pid: int
    process_create_time: float
    nonce: str
    started_at: float
    heartbeat_interval: float
    depth: int = 1
    thread: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "pid": self.pid,
            "process_create_time": self.process_create_time,
            "nonce": self.nonce,
            "timestamp": self.started_at,
            "heartbeat": time.time(),
        }


@dataclass
class _AdmissionState:
    path: Path
    pid: int
    process_create_time: float
    nonce: str
    started_at: float
    releasing: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": AIRLLM_ADMISSION_KIND,
            "pid": self.pid,
            "process_create_time": self.process_create_time,
            "nonce": self.nonce,
            "timestamp": self.started_at,
        }


def _retire_activity_owner(state: _ActivityState) -> None:
    """Make one normal owner non-live before best-effort sidecar deletion."""
    with _metadata_guard(state.path):
        tombstone = _release_tombstone_path(state.owner_path)
        with contextlib.suppress(OSError):
            _atomic_json(
                tombstone,
                {"nonce": state.nonce, "released": True, "timestamp": time.time()},
                lock_key=state.path,
            )
        owned = _read_payload_result(state.owner_path, lock_key=state.path)
        owner_removed = owned.state is _ReadState.MISSING
        if isinstance(owned.payload, dict) and owned.payload.get("nonce") == state.nonce:
            owner_removed = _unlink_with_retries(state.owner_path, lock_key=state.path)
        if owner_removed:
            _unlink_with_retries(tombstone, lock_key=state.path)


_REGISTRY_LOCK = threading.RLock()
_ACTIVE: dict[tuple[Path, int], _ActivityState] = {}
_ACTIVE_ADMISSIONS: dict[tuple[Path, int], _AdmissionState] = {}


class ResourceActivityLease:
    """Advertise a live disk/CPU-heavy operation without excluding peer operations.

    A process-local reference count makes nested entry points safe. Per-owner sidecars
    preserve visibility when multiple processes run the same kind of operation; the
    top-level JSON file remains compatible with the council's existing PID check.
    """

    def __init__(self, root: Path, kind: str, *, heartbeat: float = 1.0) -> None:
        if heartbeat <= 0:
            raise ValueError("resource activity heartbeat must be positive")
        self.path = resource_lease_path(root, kind)
        self.kind = kind
        self.heartbeat_interval = heartbeat
        self._state: _ActivityState | None = None

    def acquire(self) -> ResourceActivityLease:
        if self._state is not None:
            raise RuntimeError("resource activity lease instance is already acquired")
        pid = os.getpid()
        key = (self.path, pid)
        with _REGISTRY_LOCK:
            existing = _ACTIVE.get(key)
            if existing is not None:
                existing.depth += 1
                self._state = existing
                return self

            try:
                created = psutil.Process(pid).create_time()
            except (psutil.Error, OSError) as error:
                raise RuntimeError("could not determine resource activity owner") from error
            nonce = secrets.token_hex(16)
            owner_path = self.path.parent / ".owners" / self.kind / f"{pid}-{nonce}.json"
            state = _ActivityState(
                path=self.path,
                owner_path=owner_path,
                kind=self.kind,
                pid=pid,
                process_create_time=created,
                nonce=nonce,
                started_at=time.time(),
                heartbeat_interval=self.heartbeat_interval,
            )
            payload = state.payload()
            admission_path = _airllm_admission_path_for_resource(self.path)
            try:
                with _metadata_guard(admission_path):
                    admission_liveness = _airllm_admission_liveness(admission_path)
                    if admission_liveness is not False:
                        raise ResourceLeaseConflictError(
                            self.kind,
                            AIRLLM_ADMISSION_KIND,
                            uncertain=admission_liveness is None,
                        )
                    with _metadata_guard(self.path):
                        _atomic_json(owner_path, payload, lock_key=self.path)
                        _atomic_json(self.path, payload, lock_key=self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    _retire_activity_owner(state)
                raise
            state.thread = threading.Thread(
                target=self._heartbeat,
                args=(state,),
                name=f"resource-{self.kind}-{nonce[:8]}",
                daemon=True,
            )
            _ACTIVE[key] = state
            self._state = state
            try:
                state.thread.start()
            except BaseException:
                del _ACTIVE[key]
                self._state = None
                with contextlib.suppress(OSError):
                    _retire_activity_owner(state)
                    self._promote_another_owner(state)
                raise
        return self

    @staticmethod
    def _heartbeat(state: _ActivityState) -> None:
        while not state.stop.wait(state.heartbeat_interval):
            payload = state.payload()
            try:
                with _metadata_guard(state.path):
                    if state.stop.is_set():
                        return
                    _atomic_json(state.owner_path, payload, lock_key=state.path)
                    _atomic_json(state.path, payload, lock_key=state.path)
            except OSError:
                # A Windows reader that did not request delete sharing can block
                # os.replace briefly. Keep the lease fresh on the next interval.
                continue

    @staticmethod
    def _promote_another_owner(state: _ActivityState) -> None:
        with _metadata_guard(state.path):
            current = _read_payload(state.path, lock_key=state.path)
            if not isinstance(current, dict) or current.get("nonce") != state.nonce:
                return
            if _promote_live_owner(state.path, state.kind) is not None:
                return
            latest = _read_payload(state.path, lock_key=state.path)
            if isinstance(latest, dict) and latest.get("nonce") == state.nonce:
                _unlink_with_retries(state.path, lock_key=state.path)

    def release(self) -> None:
        with _REGISTRY_LOCK:
            state = self._state
            if state is None:
                return
            self._state = None
            if os.getpid() != state.pid:
                return
            key = (state.path, state.pid)
            current = _ACTIVE.get(key)
            if current is not state:
                return
            state.depth -= 1
            if state.depth > 0:
                return
            del _ACTIVE[key]
            state.stop.set()
        if state.thread is not None:
            state.thread.join(
                timeout=max(
                    2.0,
                    state.heartbeat_interval
                    + _LOCK_RETRY_TIMEOUT_SECONDS
                    + _IO_RETRY_TIMEOUT_SECONDS
                    + 0.5,
                )
            )
        try:
            with _metadata_guard(state.path):
                _retire_activity_owner(state)
                self._promote_another_owner(state)
        except OSError:
            # Activity markers are advisory. A cleanup failure must not mask the
            # operation's own exception; the tombstone prevents false liveness.
            return

    def __enter__(self) -> ResourceActivityLease:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


class AirLlmAdmissionLease:
    """Exclusively admit AirLLM only while no normal heavy activity is live.

    Acquisition is immediate: a live or unverifiable build, fuzz, QEMU, or peer
    AirLLM owner raises :class:`ResourceLeaseConflictError`. Admission is exclusive,
    including between independent callers in one process. PID plus process creation
    time makes a crashed or PID-reused owner recoverable by the next acquirer.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = airllm_admission_path(self.root)
        self._state: _AdmissionState | None = None

    @property
    def acquired(self) -> bool:
        """Return whether this instance still owns (or must retry releasing) admission."""
        return self._state is not None

    def acquire(self) -> AirLlmAdmissionLease:
        if self._state is not None:
            raise RuntimeError("AirLLM admission lease instance is already acquired")
        pid = os.getpid()
        key = (self.path, pid)
        with _REGISTRY_LOCK:
            existing = _ACTIVE_ADMISSIONS.get(key)
            if existing is not None:
                if existing.releasing and self._cleanup_owned_marker(existing):
                    del _ACTIVE_ADMISSIONS[key]
                else:
                    raise ResourceLeaseConflictError(
                        AIRLLM_ADMISSION_KIND,
                        AIRLLM_ADMISSION_KIND,
                        uncertain=existing.releasing,
                    )

            try:
                created = psutil.Process(pid).create_time()
            except (psutil.Error, OSError) as error:
                raise RuntimeError("could not determine AirLLM admission owner") from error
            state = _AdmissionState(
                path=self.path,
                pid=pid,
                process_create_time=created,
                nonce=secrets.token_hex(16),
                started_at=time.time(),
            )
            try:
                with _metadata_guard(self.path):
                    conflict = _first_resource_conflict(self.root)
                    if conflict is not None:
                        active_kind, uncertain = conflict
                        raise ResourceLeaseConflictError(
                            AIRLLM_ADMISSION_KIND,
                            active_kind,
                            uncertain=uncertain,
                        )
                    admission_liveness = _airllm_admission_liveness(self.path)
                    if admission_liveness is not False:
                        raise ResourceLeaseConflictError(
                            AIRLLM_ADMISSION_KIND,
                            AIRLLM_ADMISSION_KIND,
                            uncertain=admission_liveness is None,
                        )
                    _atomic_json(self.path, state.payload(), lock_key=self.path)
                _ACTIVE_ADMISSIONS[key] = state
                self._state = state
            except BaseException:
                state.releasing = True
                if not self._cleanup_owned_marker(state):
                    _ACTIVE_ADMISSIONS[key] = state
                self._state = None
                raise
        return self

    @staticmethod
    def _cleanup_owned_marker(state: _AdmissionState) -> bool:
        try:
            with _metadata_guard(state.path):
                tombstone = _release_tombstone_path(state.path)
                tombstone_written = False
                try:
                    _atomic_json(
                        tombstone,
                        {"nonce": state.nonce, "released": True, "timestamp": time.time()},
                        lock_key=state.path,
                    )
                    tombstone_written = True
                except OSError:
                    pass
                current = _read_payload_result(state.path, lock_key=state.path)
                if current.state is _ReadState.MISSING:
                    _unlink_with_retries(tombstone, lock_key=state.path)
                    return True
                if isinstance(current.payload, dict):
                    if current.payload.get("nonce") != state.nonce:
                        _unlink_with_retries(tombstone, lock_key=state.path)
                        return True
                    marker_removed = _unlink_with_retries(state.path, lock_key=state.path)
                    if marker_removed:
                        _unlink_with_retries(tombstone, lock_key=state.path)
                        return True
                    return tombstone_written
                return False
        except OSError:
            # The nonce-scoped tombstone prevents an exhausted Windows unlink from
            # presenting this process as an active admission after release.
            return False

    def release(self) -> None:
        with _REGISTRY_LOCK:
            state = self._state
            if state is None:
                return
            if os.getpid() != state.pid:
                self._state = None
                return
            key = (state.path, state.pid)
            current = _ACTIVE_ADMISSIONS.get(key)
            if current is not state:
                self._state = None
                return
            state.releasing = True
            if self._cleanup_owned_marker(state):
                del _ACTIVE_ADMISSIONS[key]
                self._state = None

    def __enter__(self) -> AirLlmAdmissionLease:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def active_resource_kind(root: Path) -> str | None:
    """Return the first live kind and repair a stale compatibility marker."""
    for kind in RESOURCE_KINDS:
        path = resource_lease_path(root, kind)
        if not path.parent.is_dir():
            continue
        try:
            with _metadata_guard(path):
                payload = _read_payload(path, lock_key=path)
                if _marker_has_live_owner(path, kind, payload):
                    return kind
                if _promote_live_owner(path, kind) is not None:
                    return kind
                if isinstance(payload, dict):
                    _unlink_with_retries(path, lock_key=path)
        except OSError:
            continue
    return None
