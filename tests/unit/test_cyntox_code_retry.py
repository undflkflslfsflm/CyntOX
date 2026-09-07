# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
import asyncio
import json
import os
from pathlib import Path

from oslab.model import CyntoxCodeWorker
from oslab.process_runner import ProcessResult, SafeProcessRunner
from oslab.schemas import utc_now


class FlakyCyntoxRunner(SafeProcessRunner):
    def __init__(self) -> None:
        super().__init__()
        self.prompt_calls = 0

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> ProcessResult:
        now = utc_now()
        if "--version" in argv:
            return ProcessResult(tuple(argv), 0, "0.22.3", "", now, now, 0, False, False)
        self.prompt_calls += 1
        if self.prompt_calls == 1:
            return ProcessResult(
                tuple(argv),
                2,
                "",
                "# Fatal error in , line 0\n# Check failed",
                now,
                now,
                0,
                False,
                False,
            )
        events = [
            {
                "type": "system",
                "subtype": "init",
                "tools": sorted(CyntoxCodeWorker.allowed_tools),
                "mcp_servers": [{"name": "oslab", "status": "connected"}],
            },
            {
                "type": "message",
                "message": {
                    "content": [{"type": "tool_use", "name": "mcp__oslab__policy_remaining_budget"}]
                },
            },
            {
                "type": "result",
                "result": "MCP_BUDGET_OK",
                "is_error": False,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ]
        return ProcessResult(tuple(argv), 0, json.dumps(events), "", now, now, 0, False, False)


def test_cyntox_code_worker_retries_native_transient_failure(tmp_path: Path) -> None:
    suffix = ".cmd" if os.name == "nt" else ""
    executable = tmp_path / "node_modules" / ".bin" / f"{'q' + 'wen'}{suffix}"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    settings = tmp_path / ".cyntox" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"$version": 4, "mcpServers": {"oslab": {"cwd": "."}}}),
        encoding="utf-8",
    )
    runner = FlakyCyntoxRunner()
    worker = CyntoxCodeWorker(
        tmp_path,
        runner,
        retry_attempts=2,
        retry_backoff_seconds=0,
    )

    response = asyncio.run(
        worker.complete(
            [{"role": "user", "content": "Use the budget tool and answer."}],
            seed=3,
            timeout=120,
        )
    )

    assert response.content == "MCP_BUDGET_OK"
    assert worker.tool_calls() == ["mcp__oslab__policy_remaining_budget"]
    assert runner.prompt_calls == 2
