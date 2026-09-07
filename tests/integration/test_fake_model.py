# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
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
