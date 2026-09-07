from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4


class QmpError(RuntimeError):
    """Raised for QMP connection, protocol, and monitor command failures."""


class QmpClient:
    def __init__(self, host: str, port: int) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("QMP client must connect through loopback")
        self.host = host
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.events: list[dict[str, Any]] = []

    async def connect(self, timeout: float = 10.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        last_error: OSError | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                break
            except OSError as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        if self.reader is None or self.writer is None:
            raise QmpError(f"QMP connection failed: {last_error}")
        greeting = await self._read_message(timeout)
        if "QMP" not in greeting:
            raise QmpError(f"invalid QMP greeting: {greeting}")
        await self.execute("qmp_capabilities", timeout=timeout)
        return greeting

    async def execute(
        self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> Any:
        if self.writer is None:
            raise QmpError("QMP is not connected")
        request_id = str(uuid4())
        request: dict[str, Any] = {"execute": command, "id": request_id}
        if arguments:
            request["arguments"] = arguments
        self.writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\r\n")
        await self.writer.drain()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"QMP command timed out: {command}")
            message = await self._read_message(remaining)
            if "event" in message:
                self.events.append(message)
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise QmpError(f"{command}: {message['error']}")
            return message.get("return")

    async def hmp(self, command_line: str, timeout: float = 30.0) -> str:
        result = await self.execute(
            "human-monitor-command", {"command-line": command_line}, timeout=timeout
        )
        return str(result or "")

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        self.reader = None
        self.writer = None

    async def _read_message(self, timeout: float) -> dict[str, Any]:
        if self.reader is None:
            raise QmpError("QMP is not connected")
        while True:
            line = await asyncio.wait_for(self.reader.readline(), timeout)
            if not line:
                raise QmpError("QMP connection closed")
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
