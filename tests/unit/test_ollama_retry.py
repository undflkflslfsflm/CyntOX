import asyncio
import json

import httpx

from oslab.config import ModelConfig
from oslab.model import OllamaProvider


def test_ollama_provider_retries_transient_5xx() -> None:
    chat_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_attempts
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={
                    "capabilities": ["completion"],
                    "details": {
                        "family": "cyntox",
                        "format": "gguf",
                        "parameter_size": "27B",
                        "quantization_level": "Q4_K_M",
                    },
                    "model_info": {"general.context_length": 8192},
                },
            )
        if request.url.path == "/api/chat":
            chat_attempts += 1
            if chat_attempts == 1:
                return httpx.Response(500, json={"error": "model temporarily busy"})
            return httpx.Response(
                200,
                json={
                    "message": {"content": json.dumps({"answer": 6})},
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                    "prompt_eval_duration": 1_000_000,
                    "eval_duration": 1_000_000,
                },
            )
        return httpx.Response(404)

    provider = OllamaProvider(
        ModelConfig(retry_backoff_seconds=0),
        transport=httpx.MockTransport(handler),
    )
    response = asyncio.run(
        provider.complete(
            [{"role": "user", "content": "Return JSON."}],
            schema={
                "type": "object",
                "properties": {"answer": {"const": 6}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        )
    )

    assert response.structured == {"answer": 6}
    assert chat_attempts == 2
