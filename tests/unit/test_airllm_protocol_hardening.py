# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import psutil
import pytest

from oslab.airllm_protocol import InvalidAirLlmResponse
from oslab.model import airllm as airllm_module
from oslab.model.airllm import (
    MAX_WORKER_REQUEST_BYTES,
    MAX_WORKER_RESPONSE_BYTES,
    AirLlmProtocolError,
    AirLlmProvider,
    AirLlmTransportError,
    AirLlmWorkerError,
)
from scripts.cyntox_airllm_worker import MAX_REQUEST_BYTES

ROOT = Path(__file__).resolve().parents[2]


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str | None = None,
    *,
    delay: float | None = None,
) -> AirLlmProvider:
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(tmp_path / "runtime"))
    if mode is None:
        monkeypatch.delenv("CYNTOX_AIRLLM_FAKE_MODE", raising=False)
    else:
        monkeypatch.setenv("CYNTOX_AIRLLM_FAKE_MODE", mode)
    if delay is None:
        monkeypatch.delenv("CYNTOX_AIRLLM_FAKE_DELAY", raising=False)
    else:
        monkeypatch.setenv("CYNTOX_AIRLLM_FAKE_DELAY", str(delay))
    return AirLlmProvider(
        ROOT,
        worker_python=Path(__import__("sys").executable),
        fake=True,
        manage_gpu_lease=False,
    )


def _wait_process_gone(process: psutil.Process, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process.is_running():
            return
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.02)
    pytest.fail(f"process {process.pid} remained alive")


def _assert_listener_closed(port: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as client:
            client.settimeout(0.1)
            if client.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.02)
    pytest.fail(f"loopback listener on port {port} remained active")


def _wait_for_diagnostics(provider: AirLlmProvider, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not provider._diagnostics and time.monotonic() < deadline:
        time.sleep(0.01)


def test_qualification_only_fault_protocol_exercises_fixed_failures_and_network_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(tmp_path / "runtime"))

    async def exercise() -> None:
        provider = AirLlmProvider(
            ROOT,
            worker_python=Path(__import__("sys").executable),
            fake=True,
            manage_gpu_lease=False,
            qualification_mode=True,
        )
        port: int | None = None
        try:
            await provider.start(timeout=5)
            port = provider.port
            assert provider._identity["qualification_mode"] is True
            assert provider._identity["offline_environment"] is True
            assert (await provider.qualification_fault("network"))["blocked"] is True
            with pytest.raises(InvalidAirLlmResponse, match="incomplete reasoning span"):
                await provider.qualification_fault("malformed")
            with pytest.raises(AirLlmWorkerError, match="out of memory"):
                await provider.qualification_fault("oom")
            with pytest.raises(AirLlmTransportError, match="TimeoutError"):
                await provider.qualification_fault("timeout", timeout=0.05)
            await asyncio.sleep(0.3)
            await provider.probe()
            with pytest.raises(AirLlmTransportError):
                await provider.qualification_fault("crash")
        finally:
            await provider.close(force=True)
        assert port is not None
        _assert_listener_closed(port)

    asyncio.run(exercise())


def test_qualification_faults_are_disabled_for_normal_providers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = _provider(monkeypatch, tmp_path)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="disabled"):
            await provider.qualification_fault("oom")

    asyncio.run(exercise())


def _raw_get(port: int, token: str | None) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _raw_generate(port: int, token: str, prompt: str) -> tuple[int, dict[str, Any]]:
    return _raw_generate_payload(
        port,
        token,
        {"prompt": prompt, "max_new_tokens": 32},
    )


def _raw_generate_payload(
    port: int, token: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_startup_crash_is_exact_and_cleans_provider_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "startup-crash")
        with pytest.raises(RuntimeError, match=r"exited before startup handshake.*code=90"):
            await provider.start(timeout=2)
        assert provider.process is None
        assert provider.port is None
        assert provider.token is None

    asyncio.run(exercise())


def test_startup_hang_honors_deadline_and_kills_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "startup-hang", delay=10)
        started = time.monotonic()
        task = asyncio.create_task(provider.start(timeout=0.1))
        await asyncio.sleep(0.03)
        assert provider.process is not None
        worker = psutil.Process(provider.process.pid)
        with pytest.raises(TimeoutError):
            await task
        assert time.monotonic() - started < 2
        assert provider.process is None
        _wait_process_gone(worker)

    asyncio.run(exercise())


def test_startup_health_probe_is_part_of_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "health-delay", delay=10)
        started = time.monotonic()
        with pytest.raises((TimeoutError, AirLlmTransportError)):
            await provider.start(timeout=0.2)
        assert time.monotonic() - started < 2
        assert provider.process is None

    asyncio.run(exercise())


def test_cancelled_startup_reaps_worker_before_propagating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "startup-hang", delay=10)
        task = asyncio.create_task(provider.start(timeout=10))
        await asyncio.sleep(0.05)
        assert provider.process is not None
        worker = psutil.Process(provider.process.pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.process is None
        _wait_process_gone(worker)

    asyncio.run(exercise())


def test_wrong_identity_rejects_handshake_and_closes_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "wrong-identity")
        observed: dict[str, Any] = {}
        validate = provider._validate_identity

        def capture(body: dict[str, Any], *, handshake: bool) -> None:
            if handshake:
                observed.update(body)
                observed["process"] = psutil.Process(int(body["pid"]))
            validate(body, handshake=handshake)

        monkeypatch.setattr(provider, "_validate_identity", capture)
        with pytest.raises(RuntimeError, match=r"identity mismatch: model_id"):
            await provider.start(timeout=2)
        _wait_process_gone(observed["process"])
        _assert_listener_closed(int(observed["port"]))
        assert provider.process is None

    asyncio.run(exercise())


def test_bad_handshake_is_protocol_error_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "bad-handshake")
        with pytest.raises(AirLlmProtocolError, match="not valid JSON"):
            await provider.start(timeout=2)
        assert provider.process is None
        assert provider.port is None
        assert provider.token is None

    asyncio.run(exercise())


def test_health_requires_authentication_and_listener_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path)
        await provider.start(timeout=2)
        assert provider.process is not None
        assert provider.port is not None
        assert provider.token is not None
        assert "token" not in provider._identity
        process = psutil.Process(provider.process.pid)
        port = provider.port
        try:
            assert await asyncio.to_thread(_raw_get, port, None) == (
                401,
                {"error": "unauthorized"},
            )
            assert await asyncio.to_thread(_raw_get, port, "wrong-token") == (
                401,
                {"error": "unauthorized"},
            )
            assert (await provider.probe()).architecture == "Qwen3_5ForConditionalGeneration"
        finally:
            await provider.close()
        _wait_process_gone(process)
        _assert_listener_closed(port)

    asyncio.run(exercise())


@pytest.mark.parametrize("invalid_budget", [True, 3.5, "32"])
def test_worker_rejects_coercive_token_budgets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_budget: object,
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path)
        await provider.start(timeout=2)
        try:
            assert provider.port is not None
            assert provider.token is not None
            status, payload = await asyncio.to_thread(
                _raw_generate_payload,
                provider.port,
                provider.token,
                {"prompt": "strict budget", "max_new_tokens": invalid_budget},
            )
            assert status == 500
            assert payload == {"error": "ValueError", "message": "worker generation failed"}
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_provider_does_not_echo_untrusted_worker_error_body() -> None:
    secret = "private prompt and hidden reasoning sentinel"

    class ErrorHandler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"error": "RuntimeError", "message": secret}).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), ErrorHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    provider = AirLlmProvider(ROOT, worker_python=Path(__import__("sys").executable), fake=True)
    provider.port = server.server_port
    provider.token = "x" * 43
    try:
        with pytest.raises(AirLlmWorkerError) as raised:
            provider._request("GET", "/health", None, 2)
        assert raised.value.code == "RuntimeError"
        assert secret not in str(raised.value)
    finally:
        server.server_close()
        thread.join(timeout=2)


def test_worker_removes_reasoning_before_authenticated_http_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path)
        await provider.start(timeout=2)
        try:
            assert provider.port is not None
            assert provider.token is not None
            status, payload = await asyncio.to_thread(
                _raw_generate, provider.port, provider.token, "test"
            )
            assert status == 200
            assert payload["text"].startswith("Fake Qwythos response")
            assert "think" not in payload["text"].casefold()
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mode", ["network-attempt", "udp-attempt", "bind-attempt", "process-attempt"]
)
def test_worker_denies_inference_egress_and_child_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, mode)
        try:
            with pytest.raises(AirLlmWorkerError) as raised:
                await provider.complete([{"role": "user", "content": "attempt"}], timeout=2)
            assert raised.value.status_code == 500
            assert await provider.probe()
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_oom_once_is_exact_and_same_worker_can_recover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "oom-once")
        try:
            with pytest.raises(AirLlmWorkerError) as raised:
                await provider.complete([{"role": "user", "content": "first"}], timeout=2)
            assert raised.value.status_code == 500
            assert raised.value.code == "RuntimeError"
            assert "out of memory" in str(raised.value).lower()
            assert provider.process is not None
            first_pid = provider.process.pid
            first_session = provider._identity["session_id"]

            response = await provider.complete(
                [{"role": "user", "content": "retry"}],
                timeout=2,
            )
            assert response.content.startswith("Fake Qwythos response")
            assert provider.process.pid == first_pid
            assert provider._identity["session_id"] == first_session
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_invalid_schema_terminates_worker_before_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "invalid-schema")
        try:
            with pytest.raises(jsonschema.ValidationError):
                await provider.complete(
                    [{"role": "user", "content": "structured"}],
                    schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                    timeout=2,
                )
            assert provider.process is None
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_stdout_flood_is_drained_without_deadlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "stdout-flood")
        try:
            response = await provider.complete(
                [{"role": "user", "content": "flood"}],
                timeout=5,
            )
            assert response.content.startswith("Fake Qwythos response")
            await asyncio.to_thread(_wait_for_diagnostics, provider)
            assert provider._diagnostics
            assert len(provider._diagnostics) <= 100
            assert all(len(line) <= 500 for line in provider._diagnostics)
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_generation_crash_closes_process_and_listener_automatically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "crash")
        await provider.start(timeout=2)
        assert provider.process is not None
        assert provider.port is not None
        process = psutil.Process(provider.process.pid)
        port = provider.port
        with pytest.raises(AirLlmTransportError, match="transport failed"):
            await provider.complete([{"role": "user", "content": "crash"}], timeout=2)
        assert provider.process is None
        _wait_process_gone(process)
        _assert_listener_closed(port)

    asyncio.run(exercise())


def test_generation_timeout_closes_process_within_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "timeout", delay=10)
        await provider.start(timeout=2)
        assert provider.process is not None
        assert provider.port is not None
        process = psutil.Process(provider.process.pid)
        port = provider.port
        started = time.monotonic()
        with pytest.raises(AirLlmTransportError, match="transport failed"):
            await provider.complete([{"role": "user", "content": "timeout"}], timeout=0.1)
        assert time.monotonic() - started < 2
        assert provider.process is None
        _wait_process_gone(process)
        _assert_listener_closed(port)

    asyncio.run(exercise())


def test_late_generation_response_is_rejected_after_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        clock = {"now": 0.0}
        closed: list[bool] = []
        provider = AirLlmProvider(
            ROOT,
            worker_python=Path(__import__("sys").executable),
            fake=True,
            manage_gpu_lease=False,
        )
        provider.process = SimpleNamespace(poll=lambda: None)  # type: ignore[assignment]
        provider._identity = {"session_id": "a" * 32}

        def late_request(*_args: object, **_kwargs: object) -> dict[str, object]:
            clock["now"] = 2.0
            return {"session_id": "a" * 32}

        async def record_close(*, force: bool = False) -> None:
            closed.append(force)
            provider.process = None

        monkeypatch.setattr(
            airllm_module,
            "time",
            SimpleNamespace(monotonic=lambda: clock["now"]),
        )
        monkeypatch.setattr(provider, "_request", late_request)
        monkeypatch.setattr(provider, "close", record_close)

        with pytest.raises(AirLlmTransportError, match="absolute deadline"):
            await provider._complete_unlocked(
                [{"role": "user", "content": "late"}],
                timeout=1.0,
            )

        assert closed == [True]

    asyncio.run(exercise())


def test_cancelled_generation_reaps_worker_and_listener_before_propagating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "timeout", delay=10)
        await provider.start(timeout=2)
        assert provider.process is not None
        assert provider.port is not None
        worker = psutil.Process(provider.process.pid)
        port = provider.port
        task = asyncio.create_task(
            provider.complete([{"role": "user", "content": "cancel"}], timeout=10)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.process is None
        _wait_process_gone(worker)
        _assert_listener_closed(port)

    asyncio.run(exercise())


@pytest.mark.parametrize("force", [False, True])
def test_spawned_descendant_is_killed_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, force: bool
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "spawned-descendant")
        await provider.start(timeout=2)
        assert provider.port is not None
        descendant = psutil.Process(int(provider._identity["test_descendant_pid"]))
        port = provider.port
        assert descendant.is_running()
        await provider.close(force=force)
        _wait_process_gone(descendant)
        _assert_listener_closed(port)

    asyncio.run(exercise())


def test_restart_reaps_stale_worker_group_before_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "spawned-descendant")
        await provider.start(timeout=2)
        assert provider.process is not None
        old_session = provider._identity["session_id"]
        old_descendant = psutil.Process(int(provider._identity["test_descendant_pid"]))
        provider.process.kill()
        await asyncio.to_thread(provider.process.wait, 5)

        try:
            response = await provider.complete([{"role": "user", "content": "restart"}], timeout=5)
            assert response.content.startswith("Fake Qwythos response")
            assert provider._identity["session_id"] != old_session
            _wait_process_gone(old_descendant)
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_provider_and_worker_enforce_request_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert MAX_WORKER_REQUEST_BYTES == MAX_REQUEST_BYTES

    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path)
        await provider.start(timeout=2)
        assert provider.port is not None
        assert provider.token is not None
        try:
            with pytest.raises(ValueError, match="request exceeds"):
                await asyncio.to_thread(
                    provider._request,
                    "POST",
                    "/generate",
                    {"prompt": "x" * MAX_WORKER_REQUEST_BYTES},
                    2,
                )

            def oversized_request() -> tuple[int, dict[str, Any]]:
                connection = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=3)
                connection.putrequest("POST", "/generate")
                connection.putheader("Authorization", f"Bearer {provider.token}")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
                connection.endheaders()
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result

            assert await asyncio.to_thread(oversized_request) == (
                413,
                {"error": "invalid_request_size"},
            )
            assert (await provider.probe()).architecture == "Qwen3_5ForConditionalGeneration"
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_worker_replaces_oversized_generation_response_with_bounded_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = _provider(monkeypatch, tmp_path, "oversized-response")
        try:
            with pytest.raises(AirLlmWorkerError) as raised:
                await provider.complete([{"role": "user", "content": "large"}], timeout=3)
            assert raised.value.status_code == 500
            assert raised.value.code == "response_too_large"
            assert len(str(raised.value)) < 500
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_provider_rejects_declared_oversized_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    del monkeypatch, tmp_path

    class OversizedHandler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(MAX_WORKER_RESPONSE_BYTES + 1))
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), OversizedHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    provider = AirLlmProvider(ROOT, worker_python=Path(__import__("sys").executable), fake=True)
    provider.port = server.server_port
    provider.token = "x" * 32
    try:
        with pytest.raises(AirLlmProtocolError, match="invalid response size"):
            provider._request("GET", "/health", None, 2)
    finally:
        server.server_close()
        thread.join(timeout=2)
