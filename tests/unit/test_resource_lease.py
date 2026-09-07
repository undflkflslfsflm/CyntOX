# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import threading
import time
import types
from pathlib import Path
from typing import Any

import psutil
import pytest

from oslab import resource_lease as resource_lease_module
from oslab.artifacts import ArtifactStore
from oslab.config import default_config
from oslab.fuzz import runner as fuzz_runner
from oslab.process_runner import SafeProcessRunner
from oslab.qemu import backend as qemu_backend
from oslab.resource_lease import (
    AirLlmAdmissionLease,
    ResourceActivityLease,
    ResourceLeaseConflictError,
    active_resource_kind,
    airllm_admission_path,
    read_resource_lease_payload,
    resource_lease_path,
)
from oslab.targets import runner as target_runner
from scripts import cyntox_council


def _hold_resource_lease(root: str, ready: object, release: object) -> None:
    ready_event = ready
    release_event = release
    with ResourceActivityLease(Path(root), "build", heartbeat=5.0):
        ready_event.set()  # type: ignore[attr-defined]
        release_event.wait(30)  # type: ignore[attr-defined]


def _hold_airllm_admission(root: str, ready: object, release: object) -> None:
    ready_event = ready
    release_event = release
    with AirLlmAdmissionLease(Path(root)):
        ready_event.set()  # type: ignore[attr-defined]
        release_event.wait(30)  # type: ignore[attr-defined]


def _attempt_resource_lease(
    root: str,
    gate_attempted: object,
    gate_acquired: object,
    finished: object,
    outcomes: object,
) -> None:
    attempted_event = gate_attempted
    acquired_event = gate_acquired
    finished_event = finished
    outcome_queue = outcomes
    real_lock = resource_lease_module._lock_metadata_handle
    gate_lock_name = f".{airllm_admission_path(Path(root)).name}.lock"

    def observed_lock(handle: Any) -> None:
        is_gate = Path(str(handle.name)).name == gate_lock_name
        if is_gate:
            attempted_event.set()  # type: ignore[attr-defined]
        real_lock(handle)
        if is_gate:
            acquired_event.set()  # type: ignore[attr-defined]

    resource_lease_module._lock_metadata_handle = observed_lock
    try:
        with ResourceActivityLease(Path(root), "build"):
            outcome_queue.put("acquired")  # type: ignore[attr-defined]
    except ResourceLeaseConflictError:
        outcome_queue.put("blocked")  # type: ignore[attr-defined]
    finally:
        finished_event.set()  # type: ignore[attr-defined]


def _attempt_airllm_admission(
    root: str,
    gate_attempted: object,
    gate_acquired: object,
    finished: object,
    outcomes: object,
) -> None:
    attempted_event = gate_attempted
    acquired_event = gate_acquired
    finished_event = finished
    outcome_queue = outcomes
    real_lock = resource_lease_module._lock_metadata_handle
    gate_lock_name = f".{airllm_admission_path(Path(root)).name}.lock"

    def observed_lock(handle: Any) -> None:
        is_gate = Path(str(handle.name)).name == gate_lock_name
        if is_gate:
            attempted_event.set()  # type: ignore[attr-defined]
        real_lock(handle)
        if is_gate:
            acquired_event.set()  # type: ignore[attr-defined]

    resource_lease_module._lock_metadata_handle = observed_lock
    try:
        with AirLlmAdmissionLease(Path(root)):
            outcome_queue.put("acquired")  # type: ignore[attr-defined]
    except ResourceLeaseConflictError:
        outcome_queue.put("blocked")  # type: ignore[attr-defined]
    finally:
        finished_event.set()  # type: ignore[attr-defined]


def _payload(path: Path) -> dict[str, Any]:
    value = read_resource_lease_payload(path)
    assert value is not None
    return value


def test_resource_activity_lease_is_nested_and_exception_safe(tmp_path: Path) -> None:
    marker = resource_lease_path(tmp_path, "build")
    outer = ResourceActivityLease(tmp_path, "build", heartbeat=0.01)
    inner = ResourceActivityLease(tmp_path, "build", heartbeat=0.01)

    with outer:
        first = _payload(marker)
        assert first["kind"] == "build"
        assert first["pid"] > 0
        assert active_resource_kind(tmp_path) == "build"
        assert cyntox_council.active_disk_heavy_workload(tmp_path) == "build"
        with inner:
            assert _payload(marker)["nonce"] == first["nonce"]
        assert marker.is_file()
        deadline = time.monotonic() + 1
        latest = _payload(marker)
        while latest["heartbeat"] <= first["heartbeat"] and time.monotonic() < deadline:
            time.sleep(0.01)
            latest = _payload(marker)
    assert latest["heartbeat"] > first["heartbeat"]

    assert not marker.exists()
    assert not list(marker.parent.rglob("*.released"))
    assert active_resource_kind(tmp_path) is None

    with (
        pytest.raises(RuntimeError, match="boom"),
        ResourceActivityLease(tmp_path, "build"),
    ):
        raise RuntimeError("boom")
    assert not marker.exists()


def test_resource_activity_lease_retries_transient_publication_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = resource_lease_path(tmp_path, "build")
    real_replace = os.replace
    remaining_conflicts = 4

    def conflicting_replace(source: str, destination: Path) -> None:
        nonlocal remaining_conflicts
        if Path(destination) == marker and marker.exists() and remaining_conflicts:
            remaining_conflicts -= 1
            raise PermissionError(13, "simulated sharing violation", str(destination))
        real_replace(source, destination)

    monkeypatch.setattr("oslab.resource_lease.os.replace", conflicting_replace)

    with ResourceActivityLease(tmp_path, "build", heartbeat=0.005):
        first = _payload(marker)
        deadline = time.monotonic() + 1
        latest = first
        while latest["heartbeat"] <= first["heartbeat"] and time.monotonic() < deadline:
            time.sleep(0.005)
            latest = _payload(marker)

    assert remaining_conflicts == 0
    assert latest["heartbeat"] > first["heartbeat"]


def test_resource_activity_lease_tolerates_stressed_concurrent_reads(tmp_path: Path) -> None:
    marker = resource_lease_path(tmp_path, "build")
    stop = threading.Event()
    failures: list[str] = []
    heartbeats: list[float] = []

    def reader(expected_nonce: str) -> None:
        while not stop.is_set():
            payload = read_resource_lease_payload(marker)
            if payload is None or payload.get("nonce") != expected_nonce:
                failures.append("reader observed missing or mismatched metadata")
                stop.set()
                return
            heartbeat = payload.get("heartbeat")
            if isinstance(heartbeat, int | float):
                heartbeats.append(float(heartbeat))

    with ResourceActivityLease(tmp_path, "build", heartbeat=0.001):
        first = _payload(marker)
        threads = [threading.Thread(target=reader, args=(first["nonce"],)) for _ in range(6)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline and not failures:
            time.sleep(0.01)
        stop.set()
        for thread in threads:
            thread.join(1)
        latest = _payload(marker)

    assert not failures
    assert len(set(heartbeats)) >= 3
    assert latest["heartbeat"] > first["heartbeat"]
    assert time.time() - latest["heartbeat"] < 0.5


def test_stopped_heartbeat_waiting_on_metadata_lock_does_not_publish(tmp_path: Path) -> None:
    marker = resource_lease_path(tmp_path, "build")
    lease = ResourceActivityLease(tmp_path, "build", heartbeat=0.005)
    lease.acquire()
    state = lease._state
    assert state is not None
    assert state.thread is not None

    with resource_lease_module._metadata_guard(marker):
        initial = _payload(marker)
        time.sleep(0.05)
        state.stop.set()
    state.thread.join(1)

    assert not state.thread.is_alive()
    assert _payload(marker)["heartbeat"] == initial["heartbeat"]
    lease.release()
    assert not marker.exists()


def test_release_tombstone_prevents_stale_live_pid_after_unlink_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = resource_lease_path(tmp_path, "build")
    lease = ResourceActivityLease(tmp_path, "build", heartbeat=5)
    lease.acquire()
    owner = next((marker.parent / ".owners" / "build").glob("*.json"))
    real_unlink = resource_lease_module._unlink_with_retries

    def blocked_unlink(path: Path, *, lock_key: Path | None = None) -> bool:
        if path in {owner, marker}:
            return False
        return real_unlink(path, lock_key=lock_key)

    monkeypatch.setattr(resource_lease_module, "_unlink_with_retries", blocked_unlink)
    lease.release()

    assert owner.exists()
    assert marker.exists()
    assert owner.with_name(f"{owner.name}.released").is_file()
    assert active_resource_kind(tmp_path) is None


def test_resource_activity_lease_preserves_other_process_owner(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_resource_lease,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(5)
        marker = resource_lease_path(tmp_path, "build")
        child_pid = process.pid
        assert child_pid is not None
        marker.write_text(
            json.dumps(
                {
                    "kind": "build",
                    "pid": 999_999_999,
                    "process_create_time": 0,
                    "heartbeat": 0,
                }
            ),
            encoding="utf-8",
        )
        assert active_resource_kind(tmp_path) == "build"
        assert _payload(marker)["pid"] == child_pid
        with ResourceActivityLease(tmp_path, "build", heartbeat=0.02):
            assert _payload(marker)["pid"] != child_pid
        assert _payload(marker)["pid"] == child_pid
        assert active_resource_kind(tmp_path) == "build"
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    assert not resource_lease_path(tmp_path, "build").exists()
    assert active_resource_kind(tmp_path) is None


def test_airllm_admission_is_exclusive_idempotent_and_blocks_new_activity(
    tmp_path: Path,
) -> None:
    marker = airllm_admission_path(tmp_path)
    admission = AirLlmAdmissionLease(tmp_path)

    with admission:
        first = _payload(marker)
        assert first["kind"] == "airllm"
        assert first["pid"] == os.getpid()
        with pytest.raises(ResourceLeaseConflictError) as peer:
            AirLlmAdmissionLease(tmp_path).acquire()
        assert peer.value.requested_kind == "airllm"
        assert peer.value.active_kind == "airllm"
        assert marker.is_file()
        with pytest.raises(ResourceLeaseConflictError) as raised:
            ResourceActivityLease(tmp_path, "build").acquire()
        assert raised.value.requested_kind == "build"
        assert raised.value.active_kind == "airllm"
        assert raised.value.uncertain is False

    admission.release()
    assert not marker.exists()
    assert not marker.with_name(f"{marker.name}.released").exists()
    with ResourceActivityLease(tmp_path, "build"):
        assert active_resource_kind(tmp_path) == "build"


@pytest.mark.parametrize("kind", ["qemu", "build", "fuzz"])
def test_airllm_admission_refuses_live_normal_activity(tmp_path: Path, kind: str) -> None:
    with ResourceActivityLease(tmp_path, kind):
        with pytest.raises(ResourceLeaseConflictError) as raised:
            AirLlmAdmissionLease(tmp_path).acquire()
        assert raised.value.requested_kind == "airllm"
        assert raised.value.active_kind == kind
        assert raised.value.uncertain is False


def test_normal_activity_kinds_remain_concurrent(tmp_path: Path) -> None:
    with (
        ResourceActivityLease(tmp_path, "build"),
        ResourceActivityLease(tmp_path, "fuzz"),
    ):
        assert resource_lease_path(tmp_path, "build").is_file()
        assert resource_lease_path(tmp_path, "fuzz").is_file()
        with pytest.raises(ResourceLeaseConflictError):
            AirLlmAdmissionLease(tmp_path).acquire()


def test_admission_recovers_pid_reuse_stale_state_in_both_directions(tmp_path: Path) -> None:
    pid = os.getpid()
    stale_created = psutil.Process(pid).create_time() + 60
    heavy_marker = resource_lease_path(tmp_path, "build")
    heavy_nonce = "a" * 32
    heavy_owner = heavy_marker.parent / ".owners" / "build" / f"{pid}-{heavy_nonce}.json"
    heavy_payload = {
        "schema_version": 1,
        "kind": "build",
        "pid": pid,
        "process_create_time": stale_created,
        "nonce": heavy_nonce,
        "timestamp": time.time(),
        "heartbeat": time.time(),
    }
    heavy_owner.parent.mkdir(parents=True)
    heavy_owner.write_text(json.dumps(heavy_payload), encoding="utf-8")
    heavy_marker.write_text(json.dumps(heavy_payload), encoding="utf-8")

    with AirLlmAdmissionLease(tmp_path):
        assert _payload(airllm_admission_path(tmp_path))["pid"] == pid
        assert not heavy_owner.exists()
        assert not heavy_marker.exists()

    admission_marker = airllm_admission_path(tmp_path)
    admission_marker.parent.mkdir(parents=True, exist_ok=True)
    admission_marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "airllm",
                "pid": pid,
                "process_create_time": stale_created,
                "nonce": "b" * 32,
                "timestamp": time.time(),
            }
        ),
        encoding="utf-8",
    )

    with ResourceActivityLease(tmp_path, "fuzz"):
        assert not admission_marker.exists()
        assert active_resource_kind(tmp_path) == "fuzz"


def test_admission_conflict_checks_fail_closed_on_unreadable_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission_marker = airllm_admission_path(tmp_path)
    real_read = resource_lease_module._read_payload_result

    with monkeypatch.context() as scoped:

        def unreadable_admission(
            path: Path, *, lock_key: Path | None = None
        ) -> resource_lease_module._ReadResult:
            if path == admission_marker:
                return resource_lease_module._ReadResult(
                    resource_lease_module._ReadState.UNREADABLE
                )
            return real_read(path, lock_key=lock_key)

        scoped.setattr(resource_lease_module, "_read_payload_result", unreadable_admission)
        with pytest.raises(ResourceLeaseConflictError) as raised:
            ResourceActivityLease(tmp_path, "build").acquire()
        assert raised.value.active_kind == "airllm"
        assert raised.value.uncertain is True

    heavy_marker = resource_lease_path(tmp_path, "build")
    owner = heavy_marker.parent / ".owners" / "build" / f"{os.getpid()}-{'c' * 32}.json"
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text("{}", encoding="utf-8")
    with monkeypatch.context() as scoped:

        def unreadable_owner(
            path: Path, *, lock_key: Path | None = None
        ) -> resource_lease_module._ReadResult:
            if path == owner:
                return resource_lease_module._ReadResult(
                    resource_lease_module._ReadState.UNREADABLE
                )
            return real_read(path, lock_key=lock_key)

        scoped.setattr(resource_lease_module, "_read_payload_result", unreadable_owner)
        with pytest.raises(ResourceLeaseConflictError) as raised:
            AirLlmAdmissionLease(tmp_path).acquire()
        assert raised.value.active_kind == "build"
        assert raised.value.uncertain is True
    owner.unlink()


def test_failed_activity_publication_tombstones_an_unremovable_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = resource_lease_path(tmp_path, "build")
    real_atomic_json = resource_lease_module._atomic_json
    real_unlink = resource_lease_module._unlink_with_retries

    def fail_marker_publication(
        path: Path, payload: dict[str, Any], *, lock_key: Path | None = None
    ) -> None:
        if path == marker:
            raise OSError("simulated compatibility-marker publication failure")
        real_atomic_json(path, payload, lock_key=lock_key)

    def retain_owner(path: Path, *, lock_key: Path | None = None) -> bool:
        if path.parent == marker.parent / ".owners" / "build" and path.suffix == ".json":
            return False
        return real_unlink(path, lock_key=lock_key)

    monkeypatch.setattr(resource_lease_module, "_atomic_json", fail_marker_publication)
    monkeypatch.setattr(resource_lease_module, "_unlink_with_retries", retain_owner)

    with pytest.raises(OSError, match="compatibility-marker"):
        ResourceActivityLease(tmp_path, "build").acquire()

    owner = next((marker.parent / ".owners" / "build").glob("*.json"))
    assert owner.with_name(f"{owner.name}.released").is_file()
    with AirLlmAdmissionLease(tmp_path):
        assert airllm_admission_path(tmp_path).is_file()


def test_airllm_release_tombstone_is_nonblocking_when_unlink_exhausts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = airllm_admission_path(tmp_path)
    admission = AirLlmAdmissionLease(tmp_path)
    admission.acquire()
    real_unlink = resource_lease_module._unlink_with_retries

    def retain_admission(path: Path, *, lock_key: Path | None = None) -> bool:
        if path == marker:
            return False
        return real_unlink(path, lock_key=lock_key)

    monkeypatch.setattr(resource_lease_module, "_unlink_with_retries", retain_admission)
    admission.release()
    admission.release()

    tombstone = marker.with_name(f"{marker.name}.released")
    assert marker.is_file()
    assert tombstone.is_file()
    with ResourceActivityLease(tmp_path, "build"):
        assert active_resource_kind(tmp_path) == "build"

    monkeypatch.undo()
    with AirLlmAdmissionLease(tmp_path):
        assert not tombstone.exists()
    assert not marker.exists()


def test_airllm_publication_interruption_rolls_back_live_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PublicationInterrupted(BaseException):
        pass

    marker = airllm_admission_path(tmp_path)
    real_atomic_json = resource_lease_module._atomic_json
    interrupted = False

    def publish_then_interrupt(
        path: Path, payload: dict[str, Any], *, lock_key: Path | None = None
    ) -> None:
        nonlocal interrupted
        real_atomic_json(path, payload, lock_key=lock_key)
        if path == marker and not interrupted:
            interrupted = True
            raise PublicationInterrupted

    monkeypatch.setattr(resource_lease_module, "_atomic_json", publish_then_interrupt)
    with pytest.raises(PublicationInterrupted):
        AirLlmAdmissionLease(tmp_path).acquire()

    assert not marker.exists()
    assert not marker.with_name(f"{marker.name}.released").exists()
    monkeypatch.undo()
    with AirLlmAdmissionLease(tmp_path):
        assert marker.is_file()


def test_airllm_failed_cleanup_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = airllm_admission_path(tmp_path)
    tombstone = marker.with_name(f"{marker.name}.released")
    admission = AirLlmAdmissionLease(tmp_path)
    admission.acquire()
    real_atomic_json = resource_lease_module._atomic_json
    real_unlink = resource_lease_module._unlink_with_retries

    def block_tombstone(
        path: Path, payload: dict[str, Any], *, lock_key: Path | None = None
    ) -> None:
        if path == tombstone:
            raise PermissionError("simulated tombstone sharing conflict")
        real_atomic_json(path, payload, lock_key=lock_key)

    def block_marker(path: Path, *, lock_key: Path | None = None) -> bool:
        if path == marker:
            return False
        return real_unlink(path, lock_key=lock_key)

    monkeypatch.setattr(resource_lease_module, "_atomic_json", block_tombstone)
    monkeypatch.setattr(resource_lease_module, "_unlink_with_retries", block_marker)
    admission.release()
    assert admission._state is not None
    assert marker.is_file()
    assert not tombstone.exists()
    with pytest.raises(ResourceLeaseConflictError) as raised:
        AirLlmAdmissionLease(tmp_path).acquire()
    assert raised.value.uncertain is True

    monkeypatch.undo()
    admission.release()
    admission.release()
    assert admission._state is None
    assert not marker.exists()
    with AirLlmAdmissionLease(tmp_path):
        assert marker.is_file()


def test_airllm_and_normal_activity_conflict_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")

    heavy_ready = context.Event()
    heavy_release = context.Event()
    heavy_process = context.Process(
        target=_hold_resource_lease,
        args=(str(tmp_path), heavy_ready, heavy_release),
    )
    heavy_process.start()
    try:
        assert heavy_ready.wait(5)
        with pytest.raises(ResourceLeaseConflictError) as raised:
            AirLlmAdmissionLease(tmp_path).acquire()
        assert raised.value.active_kind == "build"
    finally:
        heavy_release.set()
        heavy_process.join(5)
        if heavy_process.is_alive():
            heavy_process.terminate()
            heavy_process.join(5)
    assert heavy_process.exitcode == 0

    admission_ready = context.Event()
    admission_release = context.Event()
    admission_process = context.Process(
        target=_hold_airllm_admission,
        args=(str(tmp_path), admission_ready, admission_release),
    )
    admission_process.start()
    try:
        assert admission_ready.wait(5)
        with pytest.raises(ResourceLeaseConflictError) as raised:
            ResourceActivityLease(tmp_path, "qemu").acquire()
        assert raised.value.active_kind == "airllm"
    finally:
        admission_release.set()
        admission_process.join(5)
        if admission_process.is_alive():
            admission_process.terminate()
            admission_process.join(5)
    assert admission_process.exitcode == 0


def test_admission_check_and_publication_have_no_cross_process_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = threading.Event()
    continue_publication = threading.Event()
    admitted = threading.Event()
    release_admission = threading.Event()
    failures: list[BaseException] = []
    real_conflict_check = resource_lease_module._first_resource_conflict

    def paused_conflict_check(root: Path) -> tuple[str, bool] | None:
        result = real_conflict_check(root)
        checked.set()
        if not continue_publication.wait(30):
            raise TimeoutError("test did not release admission publication")
        return result

    monkeypatch.setattr(
        resource_lease_module,
        "_first_resource_conflict",
        paused_conflict_check,
    )

    def admit() -> None:
        try:
            with AirLlmAdmissionLease(tmp_path):
                admitted.set()
                release_admission.wait(30)
        except BaseException as error:  # noqa: BLE001 - collect thread failure
            failures.append(error)

    admission_thread = threading.Thread(target=admit)
    admission_thread.start()
    assert checked.wait(5)

    context = multiprocessing.get_context("spawn")
    activity_gate_attempted = context.Event()
    activity_gate_acquired = context.Event()
    activity_finished = context.Event()
    outcomes = context.Queue()
    activity_process = context.Process(
        target=_attempt_resource_lease,
        args=(
            str(tmp_path),
            activity_gate_attempted,
            activity_gate_acquired,
            activity_finished,
            outcomes,
        ),
    )
    activity_process.start()
    try:
        assert activity_gate_attempted.wait(10)
        assert not activity_gate_acquired.wait(0.2)
        continue_publication.set()
        assert admitted.wait(5)
        assert activity_gate_acquired.wait(5)
        assert activity_finished.wait(5)
        assert outcomes.get(timeout=5) == "blocked"
    finally:
        continue_publication.set()
        release_admission.set()
        admission_thread.join(5)
        activity_process.join(5)
        if activity_process.is_alive():
            activity_process.terminate()
            activity_process.join(5)

    assert not admission_thread.is_alive()
    assert failures == []
    assert activity_process.exitcode == 0
    assert not airllm_admission_path(tmp_path).exists()


def test_activity_check_and_publication_have_no_cross_process_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_to_publish = threading.Event()
    continue_publication = threading.Event()
    activity_acquired = threading.Event()
    release_activity = threading.Event()
    failures: list[BaseException] = []
    real_atomic_json = resource_lease_module._atomic_json
    paused = False

    def paused_owner_publication(
        path: Path, payload: dict[str, Any], *, lock_key: Path | None = None
    ) -> None:
        nonlocal paused
        if ".owners" in path.parts and path.parent.name == "build" and not paused:
            paused = True
            ready_to_publish.set()
            if not continue_publication.wait(30):
                raise TimeoutError("test did not release activity publication")
        real_atomic_json(path, payload, lock_key=lock_key)

    monkeypatch.setattr(resource_lease_module, "_atomic_json", paused_owner_publication)

    def hold_activity() -> None:
        try:
            with ResourceActivityLease(tmp_path, "build"):
                activity_acquired.set()
                release_activity.wait(30)
        except BaseException as error:  # noqa: BLE001 - collect thread failure
            failures.append(error)

    activity_thread = threading.Thread(target=hold_activity)
    activity_thread.start()
    assert ready_to_publish.wait(5)

    context = multiprocessing.get_context("spawn")
    admission_gate_attempted = context.Event()
    admission_gate_acquired = context.Event()
    admission_finished = context.Event()
    outcomes = context.Queue()
    admission_process = context.Process(
        target=_attempt_airllm_admission,
        args=(
            str(tmp_path),
            admission_gate_attempted,
            admission_gate_acquired,
            admission_finished,
            outcomes,
        ),
    )
    admission_process.start()
    try:
        assert admission_gate_attempted.wait(10)
        assert not admission_gate_acquired.wait(0.2)
        continue_publication.set()
        assert activity_acquired.wait(5)
        assert admission_gate_acquired.wait(5)
        assert admission_finished.wait(5)
        assert outcomes.get(timeout=5) == "blocked"
    finally:
        continue_publication.set()
        release_activity.set()
        activity_thread.join(5)
        admission_process.join(5)
        if admission_process.is_alive():
            admission_process.terminate()
            admission_process.join(5)

    assert not activity_thread.is_alive()
    assert failures == []
    assert admission_process.exitcode == 0
    assert active_resource_kind(tmp_path) is None


def test_manifest_build_advertises_activity_for_full_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = resource_lease_path(tmp_path, "build")

    async def fake_build(*_args: object, **_kwargs: object) -> dict[str, Any]:
        assert _payload(marker)["pid"] > 0
        assert active_resource_kind(tmp_path) == "build"
        return {"ok": True}

    monkeypatch.setattr(target_runner, "_run_manifest_build", fake_build)
    result = asyncio.run(
        target_runner.run_manifest_build(
            tmp_path,
            "debug",
            SafeProcessRunner(),
            ArtifactStore(tmp_path / "artifacts"),
            worktrees_root=tmp_path / ".oslab" / "worktrees",
        )
    )

    assert result == {"ok": True}
    assert not marker.exists()


def test_fixture_fuzz_advertises_activity_for_full_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = resource_lease_path(tmp_path, "fuzz")

    async def fake_fuzz(*_args: object, **_kwargs: object) -> dict[str, Any]:
        assert _payload(marker)["pid"] > 0
        assert active_resource_kind(tmp_path) == "fuzz"
        return {"iterations": 1}

    monkeypatch.setattr(fuzz_runner, "_run_fixture_fuzz", fake_fuzz)
    result = asyncio.run(
        fuzz_runner.run_fixture_fuzz(
            default_config(tmp_path), "resource-test", seed=7, total_iterations=1
        )
    )

    assert result == {"iterations": 1}
    assert not marker.exists()


def test_qemu_boot_holds_activity_until_stop_and_cleans_failed_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qemu_backend.shutil, "which", lambda _name: "docker")
    backend = qemu_backend.DockerQemuBackend(tmp_path, ArtifactStore(tmp_path / "artifacts"))
    marker = resource_lease_path(tmp_path, "qemu")

    async def fake_boot(
        _self: qemu_backend.DockerQemuBackend,
        *,
        run_id: str | None = None,
        seed: int = 1,
        test_id: str = "boot",
    ) -> dict[str, Any]:
        assert active_resource_kind(tmp_path) == "qemu"
        return {"run_id": run_id, "seed": seed, "test_id": test_id}

    backend._boot = types.MethodType(fake_boot, backend)  # type: ignore[method-assign]

    async def successful_lifecycle() -> None:
        result = await backend.boot(run_id="test", seed=9, test_id="unit")
        assert result == {"run_id": "test", "seed": 9, "test_id": "unit"}
        assert marker.is_file()
        await backend.stop()

    asyncio.run(successful_lifecycle())
    assert not marker.exists()

    async def failed_boot(
        _self: qemu_backend.DockerQemuBackend,
        **_kwargs: object,
    ) -> dict[str, Any]:
        assert marker.is_file()
        raise RuntimeError("boot failed")

    backend._boot = types.MethodType(failed_boot, backend)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="boot failed"):
        asyncio.run(backend.boot())
    assert not marker.exists()


def test_qemu_fixture_build_advertises_build_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qemu_backend.shutil, "which", lambda _name: "docker")
    backend = qemu_backend.DockerQemuBackend(tmp_path, ArtifactStore(tmp_path / "artifacts"))
    marker = resource_lease_path(tmp_path, "build")

    async def fake_build(_self: qemu_backend.DockerQemuBackend) -> dict[str, Any]:
        assert active_resource_kind(tmp_path) == "build"
        return {"ok": True}

    backend._build_fixture = types.MethodType(fake_build, backend)  # type: ignore[method-assign]

    assert asyncio.run(backend.build_fixture()) == {"ok": True}
    assert not marker.exists()


def test_qemu_exercise_holds_activity_for_full_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qemu_backend.shutil, "which", lambda _name: "docker")
    backend = qemu_backend.DockerQemuBackend(tmp_path, ArtifactStore(tmp_path / "artifacts"))
    marker = resource_lease_path(tmp_path, "qemu")

    async def fake_boot(
        _self: qemu_backend.DockerQemuBackend,
        **_kwargs: object,
    ) -> dict[str, Any]:
        assert marker.is_file()
        return {"ready": True}

    async def fake_send(_command: str) -> None:
        assert active_resource_kind(tmp_path) == "qemu"

    async def fake_wait_for(_pattern: str, _timeout: float, _occurrence: int = 1) -> str:
        assert cyntox_council.active_disk_heavy_workload(tmp_path) == "qemu"
        return 'OSLAB_EVT {"event":"PASS"}'

    backend._boot = types.MethodType(fake_boot, backend)  # type: ignore[method-assign]
    monkeypatch.setattr(backend, "send", fake_send)
    monkeypatch.setattr(backend, "wait_for", fake_wait_for)

    result = asyncio.run(backend.exercise("pass", seed=3))

    assert result.outcome.value == "PASS"
    assert not marker.exists()
