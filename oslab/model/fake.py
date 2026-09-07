from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import validate

from oslab.model.base import ModelProvider
from oslab.schemas import ModelIdentity, ModelResponse, ModelUsage, utc_now


class FakeModelProvider(ModelProvider):
    def __init__(self, scripted: list[dict[str, Any]] | None = None) -> None:
        self.scripted = list(scripted or [])
        self.calls = 0
        self.identity = ModelIdentity(
            provider="fake",
            runtime="deterministic-test-provider",
            runtime_version="1",
            model_id="fake-cyntox",
            architecture="fixture",
            parameters=1,
            quantization="none",
            format="python",
            context_limit=4096,
            endpoint="memory://fake",
            capabilities=["structured", "seed"],
        )

    async def probe(self) -> ModelIdentity:
        return self.identity

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        timeout: float | None = None,
    ) -> ModelResponse:
        del timeout
        started = utc_now()
        prompt = json.dumps(messages, sort_keys=True, separators=(",", ":"))
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        if self.scripted:
            structured = self.scripted.pop(0)
        else:
            structured = {
                "action": "inspect",
                "reason": "deterministic fake response",
                "seed": seed,
            }
        if schema is not None:
            validate(structured, schema)
        content = json.dumps(structured, sort_keys=True)
        response_hash = hashlib.sha256(content.encode()).hexdigest()
        self.calls += 1
        ended = utc_now()
        return ModelResponse(
            content=content,
            structured=structured,
            usage=ModelUsage(
                prompt_tokens=len(prompt.split()), completion_tokens=len(content.split())
            ),
            model=self.identity,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            started_at=started,
            ended_at=ended,
            seed=seed,
        )
