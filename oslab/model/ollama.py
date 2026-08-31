from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from jsonschema import ValidationError, validate

from oslab.config import ModelConfig
from oslab.model.base import ModelProvider
from oslab.schemas import ModelIdentity, ModelResponse, ModelUsage, utc_now


class OllamaProvider(ModelProvider):
    def __init__(
        self, config: ModelConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self._transport = transport
        self._identity: ModelIdentity | None = None

    async def probe(self) -> ModelIdentity:
        async with httpx.AsyncClient(
            timeout=min(self.config.timeout_seconds, 30.0), transport=self._transport
        ) as client:
            version_response = await self._request_with_retries(
                client, "GET", f"{self.config.endpoint}/api/version"
            )
            show_response = await self._request_with_retries(
                client,
                "POST",
                f"{self.config.endpoint}/api/show",
                json={"model": self.config.model_id, "verbose": False},
            )
        version = version_response.json().get("version")
        body = show_response.json()
        details = body.get("details", {})
        info = body.get("model_info", {})
        count = info.get("general.parameter_count")
        self._identity = ModelIdentity(
            provider="ollama",
            runtime="Ollama",
            runtime_version=str(version) if version is not None else None,
            model_id=self.config.model_id,
            architecture=details.get("family") or info.get("general.architecture"),
            parameters=int(count)
            if count is not None
            else self._parse_parameter_size(details.get("parameter_size")),
            quantization=details.get("quantization_level"),
            format=details.get("format"),
            context_limit=self._integer_or_none(info.get("qwen35.context_length")),
            endpoint=self.config.endpoint,
            capabilities=list(body.get("capabilities", [])),
        )
        return self._identity

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        timeout: float | None = None,
    ) -> ModelResponse:
        identity = self._identity or await self.probe()
        serialized = json.dumps(messages, sort_keys=True, separators=(",", ":"))
        prompt_hash = hashlib.sha256(serialized.encode()).hexdigest()
        options: dict[str, Any] = {
            "num_ctx": self.config.context_tokens,
            "num_predict": self.config.output_tokens,
            "temperature": self.config.temperature,
        }
        if seed is not None:
            options["seed"] = seed
        request: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
            "stream": False,
            "options": options,
            "keep_alive": "10m",
        }
        if schema is not None:
            request["format"] = schema
            request["think"] = False
        started = utc_now()
        async with httpx.AsyncClient(
            timeout=timeout or self.config.timeout_seconds, transport=self._transport
        ) as client:
            response = await self._request_with_retries(
                client, "POST", f"{self.config.endpoint}/api/chat", json=request
            )
        ended = utc_now()
        body = response.json()
        content = str(body.get("message", {}).get("content", ""))
        structured: dict[str, Any] | None = None
        if schema is not None:
            try:
                decoded = json.loads(content)
                if not isinstance(decoded, dict):
                    raise ValueError("structured response is not an object")
                validate(decoded, schema)
                structured = decoded
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise ValueError(f"invalid structured model response: {exc}") from exc
        response_hash = hashlib.sha256(content.encode()).hexdigest()
        prompt_count = int(body.get("prompt_eval_count", 0))
        eval_count = int(body.get("eval_count", 0))
        prompt_duration = int(body.get("prompt_eval_duration", 0)) / 1_000_000_000
        eval_duration = int(body.get("eval_duration", 0)) / 1_000_000_000
        return ModelResponse(
            content=content,
            structured=structured,
            usage=ModelUsage(
                prompt_tokens=prompt_count,
                completion_tokens=eval_count,
                prompt_eval_seconds=prompt_duration,
                eval_seconds=eval_duration,
            ),
            model=identity,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            started_at=started,
            ended_at=ended,
            seed=seed,
        )

    async def stream(
        self, messages: list[dict[str, str]], *, seed: int | None = None
    ) -> AsyncIterator[str]:
        options: dict[str, Any] = {"num_ctx": self.config.context_tokens}
        if seed is not None:
            options["seed"] = seed
        request = {
            "model": self.config.model_id,
            "messages": messages,
            "stream": True,
            "options": options,
        }
        async with (
            httpx.AsyncClient(
                timeout=self.config.timeout_seconds, transport=self._transport
            ) as client,
            client.stream("POST", f"{self.config.endpoint}/api/chat", json=request) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                text = chunk.get("message", {}).get("content", "")
                if text:
                    yield str(text)

    @staticmethod
    def _parse_parameter_size(value: Any) -> int | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().upper()
        try:
            if normalized.endswith("B"):
                return int(float(normalized[:-1]) * 1_000_000_000)
            return int(normalized)
        except ValueError:
            return None

    @staticmethod
    def _integer_or_none(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def _request_with_retries(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.config.retry_attempts):
            try:
                response = await client.request(method, url, **kwargs)
                if not self._is_retryable_status(response.status_code):
                    response.raise_for_status()
                    return response
                response.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and not self._is_retryable_status(
                    exc.response.status_code
                ):
                    raise
                last_error = exc
                if attempt + 1 >= self.config.retry_attempts:
                    raise
                if self.config.retry_backoff_seconds:
                    await asyncio.sleep(self.config.retry_backoff_seconds * (attempt + 1))
        raise AssertionError(f"unreachable retry loop exit: {last_error}")

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code >= 500 or status_code in {408, 409, 425, 429}
