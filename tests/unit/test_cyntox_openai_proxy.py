# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator

from oslab.mythos_prompt import PROMPT_SENTINEL, PROMPT_STATUS, PROMPT_VERSION, prompt_sha256
from scripts import cyntox_openai_proxy


def test_proxy_injects_mythos_and_disables_thinking() -> None:
    body = json.dumps(
        {
            "model": "cyntox",
            "messages": [{"role": "user", "content": "ping"}],
            "reasoning_effort": "xhigh",
            "max_tokens": 99999,
        }
    ).encode()

    injected = cyntox_openai_proxy.inject_mythos_prompt(body, "You are Mythos.")
    hardened = cyntox_openai_proxy.harden_generation_body(
        injected,
        upstream_model="cyntox:latest",
    )
    assert hardened is not None
    payload = json.loads(hardened.decode())

    content = payload["messages"][0]["content"]
    assert content.startswith("/no_think")
    assert PROMPT_SENTINEL in content
    assert payload["model"] == "cyntox:latest"
    assert payload["reasoning_effort"] == "low"
    assert payload["enable_thinking"] is False
    assert payload["thinking_budget"] == 0
    assert payload["think"] is False
    assert payload["max_tokens"] == 8192
    assert payload["options"]["num_ctx"] == 32768


def test_proxy_detects_context_errors_for_tool_strip_retry() -> None:
    payload = b'{"error":{"type":"exceed_context_size_error","message":"request exceeds the available context size"}}'

    assert cyntox_openai_proxy.is_context_error(payload)


def test_proxy_accepts_cyntox_display_casing() -> None:
    body = json.dumps(
        {"model": "CyntOX", "messages": [{"role": "user", "content": "ping"}]}
    ).encode()

    hardened = cyntox_openai_proxy.harden_generation_body(
        body,
        upstream_model="cyntox:latest",
    )
    assert hardened is not None
    payload = json.loads(hardened.decode())

    assert payload["model"] == "cyntox:latest"


def test_proxy_generation_settings_report_larger_defaults() -> None:
    assert cyntox_openai_proxy.generation_settings() == {
        "max_tokens": 8192,
        "num_ctx": 32768,
    }


def test_proxy_injection_is_exactly_once_for_launcher_and_retry_paths() -> None:
    prompt = "You are Mythos."
    wrapped = cyntox_openai_proxy.mythos_prompt_block(prompt)
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": f"{wrapped}\n\nping"}],
            "tools": [{"type": "function", "function": {"name": "read"}}],
        }
    ).encode()

    already_appended = cyntox_openai_proxy.inject_mythos_prompt(body, prompt)
    proxy_retried = cyntox_openai_proxy.inject_mythos_prompt(already_appended, prompt)
    stripped_retry = cyntox_openai_proxy.strip_native_tools(proxy_retried)

    assert already_appended == body
    assert proxy_retried == body
    assert stripped_retry is not None
    assert stripped_retry.decode().count(f"[{PROMPT_SENTINEL}]") == 1
    assert stripped_retry.decode().count(f"[/{PROMPT_SENTINEL}]") == 1


def test_proxy_replaces_stale_duplicate_and_malformed_sentinel_blocks() -> None:
    current = "Current canonical prompt."
    stale = cyntox_openai_proxy.mythos_prompt_block("Stale prompt.")
    body = json.dumps(
        {
            "messages": [
                {"role": "system", "content": f"{stale}\n\n{stale}"},
                {"role": "user", "content": f"[{PROMPT_SENTINEL}]\nping"},
            ]
        }
    ).encode()

    injected = cyntox_openai_proxy.inject_mythos_prompt(body, current)
    assert injected is not None
    rendered = injected.decode()
    assert rendered.count(f"[{PROMPT_SENTINEL}]") == 1
    assert rendered.count(f"[/{PROMPT_SENTINEL}]") == 1
    assert current in rendered
    assert "Stale prompt." not in rendered


def test_proxy_repairs_valid_block_with_inline_stray_sentinel_literals() -> None:
    prompt = "Current canonical prompt."
    wrapped = cyntox_openai_proxy.mythos_prompt_block(prompt)
    body = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": f"{wrapped}\ninline [{PROMPT_SENTINEL}] junk",
                },
                {"role": "assistant", "content": f"tail [/{PROMPT_SENTINEL}]"},
            ]
        }
    ).encode()

    injected = cyntox_openai_proxy.inject_mythos_prompt(body, prompt)
    assert injected is not None
    rendered = injected.decode()
    assert rendered.count(f"[{PROMPT_SENTINEL}]") == 1
    assert rendered.count(f"[/{PROMPT_SENTINEL}]") == 1
    assert "inline  junk" in rendered
    assert '"content":"tail"' in rendered


def test_proxy_rejects_malformed_chat_before_upstream_and_health_stays_available(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    upstream_calls: list[object] = []

    def fail_if_forwarded(request, **_kwargs):  # type: ignore[no-untyped-def]
        upstream_calls.append(request)
        raise AssertionError("malformed chat request reached upstream")

    monkeypatch.setattr(cyntox_openai_proxy.urllib.request, "urlopen", fail_if_forwarded)
    server = cyntox_openai_proxy.CyntOXProxy(
        ("127.0.0.1", 0),
        cyntox_openai_proxy.Handler,
        target_base="http://127.0.0.1:1",
        retries=0,
        system_prompt="Current prompt",
        upstream_model="cyntox:latest",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        invalid_bodies = (
            b'{"messages": [',
            json.dumps({"messages": [{"role": "user", "content": {"text": "coerce me"}}]}).encode(),
            json.dumps({"messages": [{"role": 7, "content": "bad role"}]}).encode(),
        )
        for invalid_body in invalid_bodies:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=invalid_body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            error_payload = json.loads(response.read().decode())
            connection.close()

            assert response.status == 400
            assert error_payload["error"]["type"] == "invalid_request_error"
        assert upstream_calls == []

        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", "/__cyntox_proxy_health")
        health_response = connection.getresponse()
        health = json.loads(health_response.read().decode())
        connection.close()

        assert health_response.status == 200
        assert health["ok"] is True
        assert health["prompt_version"] == PROMPT_VERSION
        assert upstream_calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_proxy_injection_rejects_malformed_json() -> None:
    body = b'{"messages": ['
    assert cyntox_openai_proxy.inject_mythos_prompt(body, "Current prompt") is None


def test_handler_calls_prompt_injection_once_for_valid_chat(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    real_inject = cyntox_openai_proxy.inject_mythos_prompt
    injection_calls: list[bytes | None] = []
    upstream_bodies: list[bytes | None] = []

    def counted_inject(body: bytes | None, system_prompt: str) -> bytes | None:
        injection_calls.append(body)
        return real_inject(body, system_prompt)

    class FakeUpstream:
        status = 200
        headers: dict[str, str] = {"Content-Type": "application/json"}

        def __init__(self) -> None:
            self._chunks: Iterator[bytes] = iter((b"{}", b""))

        def __enter__(self) -> FakeUpstream:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            return next(self._chunks)

    def fake_urlopen(request, **_kwargs):  # type: ignore[no-untyped-def]
        upstream_bodies.append(request.data)
        return FakeUpstream()

    monkeypatch.setattr(cyntox_openai_proxy, "inject_mythos_prompt", counted_inject)
    monkeypatch.setattr(cyntox_openai_proxy.urllib.request, "urlopen", fake_urlopen)
    server = cyntox_openai_proxy.CyntOXProxy(
        ("127.0.0.1", 0),
        cyntox_openai_proxy.Handler,
        target_base="http://127.0.0.1:1",
        retries=0,
        system_prompt="Current prompt",
        upstream_model="cyntox:latest",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps({"messages": [{"role": "user", "content": "ping"}]}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()

        assert response.status == 200
        assert len(injection_calls) == 1
        assert len(upstream_bodies) == 1
        assert upstream_bodies[0] is not None
        assert upstream_bodies[0].decode().count(f"[{PROMPT_SENTINEL}]") == 1
        assert upstream_bodies[0].decode().count(f"[/{PROMPT_SENTINEL}]") == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_proxy_waits_for_gpu_lease_before_reaching_generation_upstream(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    lease_entered = threading.Event()
    allow_lease = threading.Event()
    upstream_called = threading.Event()

    class BlockingLease:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def acquire(self) -> BlockingLease:
            lease_entered.set()
            assert allow_lease.wait(timeout=5)
            return self

        def release(self) -> None:
            pass

    class FakeUpstream:
        status = 200
        headers: dict[str, str] = {"Content-Type": "application/json"}

        def __enter__(self) -> FakeUpstream:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            return b""

    def fake_urlopen(*_args: object, **_kwargs: object) -> FakeUpstream:
        upstream_called.set()
        return FakeUpstream()

    monkeypatch.setattr(cyntox_openai_proxy, "GpuLease", BlockingLease)
    monkeypatch.setattr(cyntox_openai_proxy.urllib.request, "urlopen", fake_urlopen)
    server = cyntox_openai_proxy.CyntOXProxy(
        ("127.0.0.1", 0),
        cyntox_openai_proxy.Handler,
        target_base="http://127.0.0.1:1",
        retries=0,
        system_prompt="Current prompt",
        upstream_model="cyntox:latest",
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    client_done = threading.Event()
    response_status: list[int] = []

    def request() -> None:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps({"messages": [{"role": "user", "content": "ping"}]}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            response_status.append(response.status)
        finally:
            connection.close()
            client_done.set()

    client_thread = threading.Thread(target=request, daemon=True)
    client_thread.start()
    try:
        assert lease_entered.wait(timeout=2)
        assert upstream_called.wait(timeout=0.1) is False
        allow_lease.set()
        assert client_done.wait(timeout=5)
        assert upstream_called.is_set()
        assert response_status == [200]
    finally:
        allow_lease.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        client_thread.join(timeout=5)


def test_proxy_gpu_lease_timeout_fails_without_calling_upstream(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    upstream_calls: list[object] = []

    class TimeoutLease:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def acquire(self) -> TimeoutLease:
            raise TimeoutError("busy")

        def release(self) -> None:
            raise AssertionError("an unacquired lease must not be released")

    monkeypatch.setattr(cyntox_openai_proxy, "GpuLease", TimeoutLease)
    monkeypatch.setattr(
        cyntox_openai_proxy.urllib.request,
        "urlopen",
        lambda request, **_kwargs: upstream_calls.append(request),
    )
    server = cyntox_openai_proxy.CyntOXProxy(
        ("127.0.0.1", 0),
        cyntox_openai_proxy.Handler,
        target_base="http://127.0.0.1:1",
        retries=0,
        system_prompt="Current prompt",
        upstream_model="cyntox:latest",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps({"messages": [{"role": "user", "content": "ping"}]}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        error = json.loads(response.read().decode("utf-8"))
        connection.close()

        assert response.status == 503
        assert error["error"]["type"] == "gpu_lease_timeout"
        assert upstream_calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_proxy_health_binds_normalized_prompt_version_and_hash() -> None:
    server = cyntox_openai_proxy.CyntOXProxy(
        ("127.0.0.1", 0),
        cyntox_openai_proxy.Handler,
        target_base="http://127.0.0.1:11434",
        retries=0,
        system_prompt="  Current prompt.\n",
        upstream_model="cyntox:latest",
    )
    try:
        health = cyntox_openai_proxy.proxy_health_payload(server)
        assert health["prompt_version"] == PROMPT_VERSION
        assert health["prompt_status"] == PROMPT_STATUS
        assert health["prompt_sha256"] == prompt_sha256("Current prompt.")
    finally:
        server.server_close()
