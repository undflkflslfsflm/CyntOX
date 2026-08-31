import asyncio

from oslab.model import FakeModelProvider


def test_fake_provider_structured_contract() -> None:
    provider = FakeModelProvider([{"status": "ok"}])
    schema = {
        "type": "object",
        "properties": {"status": {"const": "ok"}},
        "required": ["status"],
        "additionalProperties": False,
    }
    response = asyncio.run(
        provider.complete([{"role": "user", "content": "test"}], schema=schema, seed=1)
    )
    assert response.structured == {"status": "ok"}
    assert response.seed == 1
