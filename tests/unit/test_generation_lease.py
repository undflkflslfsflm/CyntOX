# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from oslab import generation_lease
from oslab.config import ModelConfig
from oslab.gpu_lease import GpuLease, default_gpu_lease_path
from oslab.model import airllm as airllm_module
from oslab.model import cyntox_code as cyntox_code_module
from oslab.model import ollama as ollama_module
from oslab.model.airllm import AirLlmProvider
from oslab.model.cyntox_code import CyntoxCodeWorker
from oslab.model.ollama import OllamaProvider
from oslab.process_runner import ProcessResult
from oslab.resource_lease import ResourceActivityLease, ResourceLeaseConflictError
from oslab.schemas import ModelIdentity, utc_now


def _multiprocess_lease_worker(
    lease_dir: str,
    gpu_uuid: str,
    start_event: Any,
    observations: Any,
) -> None:
    os.environ["CYNTOX_GPU_LEASE_DIR"] = lease_dir
    os.environ["CYNTOX_GPU_UUID"] = gpu_uuid

    async def exercise() -> None:
        if not start_event.wait(10):
            raise TimeoutError("multiprocess lease test did not receive its start signal")
        async with generation_lease.gpu_generation_lease(timeout=10):
            observations.put(("entered", os.getpid(), time.monotonic_ns()))
            await asyncio.sleep(0.1)
            observations.put(("exiting", os.getpid(), time.monotonic_ns()))

    asyncio.run(exercise())


def _identity(provider: str) -> ModelIdentity:
    return ModelIdentity(
        provider=provider,
        runtime=provider,
        runtime_version="test",
        model_id="test-model",
        architecture="test",
        endpoint="http://127.0.0.1:11434",
    )


def _cyntox_result(content: str = "CyntOX lease held") -> ProcessResult:
    now = utc_now()
    events = [
        {
            "type": "system",
            "subtype": "init",
            "tools": sorted(CyntoxCodeWorker.allowed_tools),
            "mcp_servers": [{"name": "oslab", "status": "connected"}],
        },
        {
            "type": "result",
            "result": content,
            "is_error": False,
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    ]
    return ProcessResult(
        ("fake-cyntox-code",),
        0,
        json.dumps(events),
        "",
        now,
        now,
        0,
        False,
        False,
    )


class LeaseCheckingStream(httpx.AsyncByteStream):
    def __init__(self, state: dict[str, int]) -> None:
        self.state = state

    async def __aiter__(self) -> AsyncIterator[bytes]:
        assert self.state["owners"] == 1
        yield b'{"message":{"content":"stream "}}\n'
        assert self.state["owners"] == 1
        yield b'{"message":{"content":"held"}}\n'

    async def aclose(self) -> None:
        return None


def test_gpu_generation_lease_is_exclusive_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    observations = context.Queue()
    gpu_uuid = "GPU-generation-lease-multiprocess-test"
    processes = [
        context.Process(
            target=_multiprocess_lease_worker,
            args=(str(tmp_path), gpu_uuid, start_event, observations),
        )
        for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        messages = [observations.get(timeout=20) for _ in range(len(processes) * 2)]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        observations.close()
        observations.join_thread()

    intervals: dict[int, dict[str, int]] = {}
    for kind, pid, timestamp in messages:
        intervals.setdefault(pid, {})[kind] = timestamp
    assert len(intervals) == len(processes)
    ordered = sorted((value["entered"], value["exiting"]) for value in intervals.values())
    assert all(
        exit_time <= next_enter
        for (_, exit_time), (next_enter, _) in zip(ordered, ordered[1:], strict=False)
    )


def test_direct_launcher_helper_waits_for_machine_gpu_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease_dir = tmp_path / "leases"
    marker = tmp_path / "child-started.txt"
    gpu_uuid = "GPU-direct-launcher-helper-test"
    monkeypatch.setenv("CYNTOX_GPU_LEASE_DIR", str(lease_dir))
    monkeypatch.setenv("CYNTOX_GPU_UUID", gpu_uuid)
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "cyntox_gpu_lease_exec.py"
    child_code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')"
    environment = dict(os.environ)
    owner = GpuLease(default_gpu_lease_path(), timeout=2)
    owner.acquire()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed local Python and helper
            [
                sys.executable,
                str(wrapper),
                "--",
                sys.executable,
                "-c",
                child_code,
                str(marker),
            ],
            cwd=wrapper.parents[1],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.4)
        assert process.poll() is None
        assert marker.exists() is False
        owner.release()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr or stdout
        assert marker.read_text(encoding="utf-8") == "started"
    finally:
        owner.release()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_cancellation_releases_a_lease_acquired_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquire_started = threading.Event()
    allow_acquire = threading.Event()
    instances: list[Any] = []

    class DelayedLease:
        def __init__(self, _path: Path, *, timeout: float) -> None:
            self.timeout = timeout
            self.acquired = False
            self.released = False
            instances.append(self)

        def acquire(self) -> DelayedLease:
            acquire_started.set()
            if not allow_acquire.wait(5):
                raise TimeoutError("test did not permit delayed acquisition")
            self.acquired = True
            return self

        def release(self) -> None:
            self.released = True

    monkeypatch.setenv("CYNTOX_GPU_LEASE_DIR", str(tmp_path))
    monkeypatch.setenv("CYNTOX_GPU_UUID", "GPU-cancellation-test")
    monkeypatch.setattr(generation_lease, "GpuLease", DelayedLease)

    async def exercise() -> None:
        async def wait_for_lease() -> None:
            async with generation_lease.gpu_generation_lease(timeout=2):
                raise AssertionError("cancelled waiter must never enter generation")

        waiter = asyncio.create_task(wait_for_lease())
        assert await asyncio.to_thread(acquire_started.wait, 2)
        waiter.cancel()
        allow_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=2)
        assert instances[0].acquired is True
        assert instances[0].released is True

        async with generation_lease.gpu_generation_lease(timeout=2):
            assert instances[1].acquired is True
            assert instances[1].released is False
        assert instances[1].released is True

    asyncio.run(exercise())


def test_ollama_complete_and_stream_hold_generation_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"owners": 0}
    acquisitions: list[tuple[bool, float]] = []

    @asynccontextmanager
    async def tracked_lease(*, enabled: bool, timeout: float) -> AsyncIterator[None]:
        acquisitions.append((enabled, timeout))
        assert enabled is True
        assert state["owners"] == 0
        state["owners"] += 1
        try:
            yield
        finally:
            state["owners"] -= 1

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        assert state["owners"] == 1
        body = json.loads(request.content)
        if body["stream"]:
            return httpx.Response(200, stream=LeaseCheckingStream(state))
        return httpx.Response(
            200,
            json={
                "message": {"content": "complete held"},
                "prompt_eval_count": 2,
                "eval_count": 3,
            },
        )

    monkeypatch.setattr(ollama_module, "gpu_generation_lease", tracked_lease)
    provider = OllamaProvider(ModelConfig(), transport=httpx.MockTransport(handler))
    provider._identity = _identity("ollama")

    async def exercise() -> None:
        completed = await provider.complete([{"role": "user", "content": "complete"}])
        streamed = [
            chunk async for chunk in provider.stream([{"role": "user", "content": "stream"}])
        ]
        assert completed.content == "complete held"
        assert streamed == ["stream ", "held"]

    asyncio.run(exercise())
    assert state["owners"] == 0
    assert len(acquisitions) == 2


def test_cyntox_code_complete_holds_generation_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"owners": 0, "generation_calls": 0}

    @asynccontextmanager
    async def tracked_lease(*, enabled: bool, timeout: float) -> AsyncIterator[None]:
        del timeout
        assert enabled is True
        state["owners"] += 1
        try:
            yield
        finally:
            state["owners"] -= 1

    worker = CyntoxCodeWorker(tmp_path)
    worker._identity = _identity("cyntox-code")
    monkeypatch.setattr(worker, "_write_worker_settings", lambda: None)

    async def fake_run(*_args: object, **_kwargs: object) -> ProcessResult:
        assert state["owners"] == 1
        state["generation_calls"] += 1
        return _cyntox_result()

    monkeypatch.setattr(cyntox_code_module, "gpu_generation_lease", tracked_lease)
    monkeypatch.setattr(worker, "_run_with_retries", fake_run)

    response = asyncio.run(worker.complete([{"role": "user", "content": "hold the lease"}]))

    assert response.content == "CyntOX lease held"
    assert state == {"owners": 0, "generation_calls": 1}


def test_airllm_complete_holds_generation_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"owners": 0, "generation_calls": 0}

    @asynccontextmanager
    async def tracked_lease(*, enabled: bool, timeout: float) -> AsyncIterator[None]:
        assert enabled is True
        assert 0 < timeout <= 2
        state["owners"] += 1
        try:
            yield
        finally:
            state["owners"] -= 1

    provider = AirLlmProvider(
        tmp_path,
        worker_python=Path(__import__("sys").executable),
        fake=True,
    )
    sentinel = object()

    async def fake_complete(*_args: object, **_kwargs: object) -> object:
        assert state["owners"] == 1
        with pytest.raises(ResourceLeaseConflictError):
            ResourceActivityLease(tmp_path, "build").acquire()
        state["generation_calls"] += 1
        return sentinel

    monkeypatch.setattr(airllm_module, "gpu_generation_lease", tracked_lease)
    monkeypatch.setattr(provider, "_complete_unlocked", fake_complete)

    response = asyncio.run(
        provider.complete([{"role": "user", "content": "hold the lease"}], timeout=2)
    )

    assert response is sentinel
    assert state == {"owners": 0, "generation_calls": 1}
    with pytest.raises(ResourceLeaseConflictError):
        ResourceActivityLease(tmp_path, "build").acquire()
    asyncio.run(provider.close())
    with ResourceActivityLease(tmp_path, "build"):
        pass


def test_airllm_cancellation_releases_admission_acquired_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquire_started = threading.Event()
    allow_acquire = threading.Event()
    instances: list[Any] = []

    class DelayedAdmission:
        def __init__(self, observed_root: Path) -> None:
            assert observed_root == tmp_path
            self.acquired = False
            self.released = False
            instances.append(self)

        def acquire(self) -> DelayedAdmission:
            acquire_started.set()
            if not allow_acquire.wait(5):
                raise TimeoutError("test did not permit delayed admission")
            self.acquired = True
            return self

        def release(self) -> None:
            self.released = True
            self.acquired = False

    monkeypatch.setattr(airllm_module, "AirLlmAdmissionLease", DelayedAdmission)
    provider = AirLlmProvider(tmp_path, worker_python=Path(__import__("sys").executable), fake=True)

    async def exercise() -> None:
        waiter = asyncio.create_task(
            provider.complete([{"role": "user", "content": "cancel admission"}], timeout=2)
        )
        assert await asyncio.to_thread(acquire_started.wait, 2)
        waiter.cancel()
        allow_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=2)

    asyncio.run(exercise())
    assert len(instances) == 1
    assert instances[0].released is True
    assert provider._resource_admission is None


def test_airllm_dead_worker_cleanup_keeps_admission_for_replacement(tmp_path: Path) -> None:
    provider = AirLlmProvider(tmp_path, worker_python=Path(sys.executable), fake=True)
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait(timeout=5)
    provider.process = process
    assert provider._acquire_resource_admission_sync() is True

    asyncio.run(provider._close_impl(force=True, release_admission=False))

    assert provider.process is None
    with pytest.raises(ResourceLeaseConflictError):
        ResourceActivityLease(tmp_path, "build").acquire()
    asyncio.run(provider.close(force=True))
    with ResourceActivityLease(tmp_path, "build"):
        pass


def test_ollama_zero_timeout_fails_before_lease_or_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"lease_calls": 0, "generation_calls": 0}

    @asynccontextmanager
    async def forbidden_lease(*, enabled: bool, timeout: float) -> AsyncIterator[None]:
        del enabled, timeout
        state["lease_calls"] += 1
        yield

    def handler(_request: httpx.Request) -> httpx.Response:
        state["generation_calls"] += 1
        return httpx.Response(500)

    monkeypatch.setattr(ollama_module, "gpu_generation_lease", forbidden_lease)
    provider = OllamaProvider(ModelConfig(), transport=httpx.MockTransport(handler))
    provider._identity = _identity("ollama")

    with pytest.raises(TimeoutError, match="deadline already expired"):
        asyncio.run(provider.complete([{"role": "user", "content": "never run"}], timeout=0))
    assert state == {"lease_calls": 0, "generation_calls": 0}


def test_cyntox_code_zero_timeout_fails_before_lease_or_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"lease_calls": 0, "generation_calls": 0}

    @asynccontextmanager
    async def forbidden_lease(*, enabled: bool, timeout: float) -> AsyncIterator[None]:
        del enabled, timeout
        state["lease_calls"] += 1
        yield

    worker = CyntoxCodeWorker(tmp_path)
    worker._identity = _identity("cyntox-code")
    monkeypatch.setattr(worker, "_write_worker_settings", lambda: None)

    async def forbidden_run(*_args: object, **_kwargs: object) -> ProcessResult:
        state["generation_calls"] += 1
        return _cyntox_result()

    monkeypatch.setattr(cyntox_code_module, "gpu_generation_lease", forbidden_lease)
    monkeypatch.setattr(worker, "_run_with_retries", forbidden_run)

    with pytest.raises(TimeoutError, match="deadline already expired"):
        asyncio.run(worker.complete([{"role": "user", "content": "never run"}], timeout=0))
    assert state == {"lease_calls": 0, "generation_calls": 0}


def test_manage_gpu_lease_false_skips_acquisition_for_all_provider_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ForbiddenLease:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError(
                "GpuLease must not be constructed when lease management is disabled"
            )

    monkeypatch.setenv("CYNTOX_GPU_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("CYNTOX_GPU_UUID", "GPU-disabled-test")
    monkeypatch.setattr(generation_lease, "GpuLease", ForbiddenLease)

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["stream"]:
            return httpx.Response(200, stream=LeaseCheckingStream({"owners": 1}))
        return httpx.Response(200, json={"message": {"content": "complete"}})

    ollama = OllamaProvider(
        ModelConfig(),
        transport=httpx.MockTransport(ollama_handler),
        manage_gpu_lease=False,
    )
    ollama._identity = _identity("ollama")

    worker = CyntoxCodeWorker(tmp_path, manage_gpu_lease=False)
    worker._identity = _identity("cyntox-code")
    monkeypatch.setattr(worker, "_write_worker_settings", lambda: None)

    async def fake_cyntox_run(*_args: object, **_kwargs: object) -> ProcessResult:
        return _cyntox_result("disabled path completed")

    monkeypatch.setattr(worker, "_run_with_retries", fake_cyntox_run)

    async def exercise() -> None:
        complete = await ollama.complete([{"role": "user", "content": "complete"}])
        streamed = [chunk async for chunk in ollama.stream([{"role": "user", "content": "stream"}])]
        code = await worker.complete([{"role": "user", "content": "code"}])
        assert complete.content == "complete"
        assert streamed == ["stream ", "held"]
        assert code.content == "disabled path completed"

    asyncio.run(exercise())
    assert not default_gpu_lease_path("GPU-disabled-test", base_dir=tmp_path / "leases").exists()
