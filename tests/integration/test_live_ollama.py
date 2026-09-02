import asyncio

import pytest

from oslab.config import default_config
from oslab.model import OllamaProvider


@pytest.mark.live
def test_live_local_cyntox_structured_response() -> None:
    provider = OllamaProvider(default_config().model)
    schema = {
        "type": "object",
        "properties": {"answer": {"const": 6}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    response = asyncio.run(
        provider.complete(
            [{"role": "user", "content": "Return a JSON object whose answer is 3+3."}],
            schema=schema,
            seed=3,
        )
    )
    assert response.structured == {"answer": 6}
    assert response.model.provider == "ollama"
