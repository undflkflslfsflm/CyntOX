from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from oslab.schemas import ModelIdentity, ModelResponse


class ModelProvider(ABC):
    async def start(self, *, timeout: float | None = None) -> None:
        del timeout

    @abstractmethod
    async def probe(self) -> ModelIdentity:
        raise AssertionError("abstract ModelProvider.probe called")

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        timeout: float | None = None,
    ) -> ModelResponse:
        raise AssertionError("abstract ModelProvider.complete called")

    async def stream(
        self, messages: list[dict[str, str]], *, seed: int | None = None
    ) -> AsyncIterator[str]:
        response = await self.complete(messages, seed=seed)
        yield response.content

    async def health(self) -> bool:
        try:
            await self.probe()
            return True
        except (OSError, TimeoutError, ValueError):
            return False

    async def close(self) -> None:
        return None
