from __future__ import annotations

import json

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
        upstream_model="huihui-qwen3.8-27b-abliterated:latest",
    )
    assert hardened is not None
    payload = json.loads(hardened.decode())

    content = payload["messages"][0]["content"]
    assert content.startswith("/no_think")
    assert "CYNTOX_MYTHOS_SYSTEM_PROMPT_V1" in content
    assert payload["model"] == "huihui-qwen3.8-27b-abliterated:latest"
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
        upstream_model="huihui-qwen3.8-27b-abliterated:latest",
    )
    assert hardened is not None
    payload = json.loads(hardened.decode())

    assert payload["model"] == "huihui-qwen3.8-27b-abliterated:latest"


def test_proxy_generation_settings_report_larger_defaults() -> None:
    assert cyntox_openai_proxy.generation_settings() == {
        "max_tokens": 8192,
        "num_ctx": 32768,
    }
