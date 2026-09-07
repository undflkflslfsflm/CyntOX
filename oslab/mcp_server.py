from __future__ import annotations

import json
import sys
from typing import Any

TOOLS = [
    {
        "name": "policy_remaining_budget",
        "description": "Return the fixed external budget visible to the worker; this tool has no side effects.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "fixture_explain",
        "description": "Explain one allowlisted fixture test mode without running host commands.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"enum": ["pass", "fail", "crash", "hang", "snapshot", "seeded"]}
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cyntox-os-lab-broker", "version": "0.1.0"},
            },
        )
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "tools/list":
        return _response(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "policy_remaining_budget":
            value = {
                "tool_calls": 4,
                "wall_seconds": 120,
                "network": "guest-disabled",
                "host_shell": "unavailable",
            }
        elif name == "fixture_explain" and arguments.get("mode") in {
            "pass",
            "fail",
            "crash",
            "hang",
            "snapshot",
            "seeded",
        }:
            value = {
                "mode": arguments["mode"],
                "target": "boot-sector",
                "transport": "serial",
                "network": "none",
            }
        else:
            return _error(request_id, -32602, "unknown tool or schema-invalid arguments")
        encoded = json.dumps(value, sort_keys=True)
        return _response(
            request_id,
            {
                "content": [{"type": "text", "text": encoded}],
                "structuredContent": value,
                "isError": False,
            },
        )
    if request_id is None:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
