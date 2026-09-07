# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
import asyncio
from pathlib import Path

import pytest

from oslab.model import CyntoxCodeWorker


@pytest.mark.live
def test_cyntox_code_uses_only_brokered_mcp_tool() -> None:
    worker = CyntoxCodeWorker(Path.cwd())
    response = asyncio.run(
        worker.complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Invoke mcp__oslab__policy_remaining_budget exactly once, then reply "
                        "exactly MCP_BUDGET_OK."
                    ),
                }
            ],
            seed=3,
            timeout=120,
        )
    )
    assert response.content == "MCP_BUDGET_OK"
    assert worker.tool_calls() == ["mcp__oslab__policy_remaining_budget"]
    assert set(worker.last_events[0]["tools"]) == worker.allowed_tools
    assert worker.last_events[0]["mcp_servers"] == [{"name": "oslab", "status": "connected"}]
