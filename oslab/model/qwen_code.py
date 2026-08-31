from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from oslab.model.base import ModelProvider
from oslab.process_runner import SafeProcessRunner
from oslab.schemas import ModelIdentity, ModelResponse, ModelUsage, utc_now


class QwenCodeWorker(ModelProvider):
    allowed_tools = {
        "mcp__oslab__policy_remaining_budget",
        "mcp__oslab__fixture_explain",
    }

    def __init__(self, project_root: Path, runner: SafeProcessRunner | None = None) -> None:
        self.project_root = project_root.resolve()
        suffix = ".cmd" if os.name == "nt" else ""
        self.executable = self.project_root / "node_modules" / ".bin" / f"qwen{suffix}"
        node = shutil.which("node")
        if node is None and os.name == "nt":
            bundled = (
                Path.home()
                / ".cache"
                / "codex-runtimes"
                / "codex-primary-runtime"
                / "dependencies"
                / "node"
                / "bin"
                / "node.exe"
            )
            node = str(bundled) if bundled.is_file() else None
        self.node_dir = str(Path(node).parent) if node else None
        self.runner = runner or SafeProcessRunner()
        self._identity: ModelIdentity | None = None
        self.last_events: list[dict[str, Any]] = []

    async def probe(self) -> ModelIdentity:
        if not self.executable.is_file():
            raise FileNotFoundError(self.executable)
        result = await self.runner.run(
            [str(self.executable), "--version"],
            cwd=self.project_root,
            timeout=20,
            env={"PATH": self._path_env()},
        )
        if result.returncode != 0:
            raise OSError(result.stderr or result.stdout)
        self._identity = ModelIdentity(
            provider="qwen-code",
            runtime="Qwen Code",
            runtime_version=result.stdout.strip(),
            model_id="qwen-os-lab-worker:latest",
            architecture="qwen35",
            parameters=27_320_697_856,
            quantization="Q4_K_M",
            format="gguf-via-ollama",
            context_limit=16_384,
            endpoint="http://127.0.0.1:11434/v1",
            capabilities=["headless", "json-events", "mcp", "budgets"],
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
        prompt = "\n\n".join(f"{item['role'].upper()}: {item['content']}" for item in messages)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        argv = [str(self.executable), "-p", prompt, "--output-format", "json"]
        if schema is not None:
            argv.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
        started = utc_now()
        result = await self.runner.run(
            argv,
            cwd=self.project_root,
            timeout=timeout or 120,
            env={
                "OSLAB_PYTHON": sys.executable,
                "PATH": self._path_env(),
                "QWEN_RUNTIME_DIR": str(self.project_root / ".oslab" / "qwen-code"),
                "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
                "OSLAB_SEED": str(seed) if seed is not None else "",
            },
        )
        ended = utc_now()
        if result.timed_out:
            raise TimeoutError("Qwen Code worker exceeded the external wall-clock budget")
        if result.returncode != 0:
            raise OSError(result.stderr or result.stdout)
        try:
            events = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Qwen Code did not emit a JSON event array: {exc}") from exc
        if not isinstance(events, list) or not events:
            raise ValueError("Qwen Code emitted no events")
        if not all(isinstance(event, dict) for event in events):
            raise ValueError("Qwen Code emitted a non-object event")
        self.last_events = events
        init = events[0]
        if init.get("type") != "system" or init.get("subtype") != "init":
            raise ValueError("Qwen Code stream has no init event")
        declared_tools = set(init.get("tools", []))
        if declared_tools != self.allowed_tools:
            unexpected = sorted(declared_tools - self.allowed_tools)
            missing = sorted(self.allowed_tools - declared_tools)
            raise PermissionError(
                f"Qwen Code tool boundary mismatch: unexpected={unexpected}, missing={missing}"
            )
        servers = init.get("mcp_servers", [])
        if not any(
            isinstance(server, dict)
            and server.get("name") == "oslab"
            and server.get("status") == "connected"
            for server in servers
        ):
            raise ConnectionError("Qwen Code did not connect the oslab MCP broker")
        final = events[-1]
        if not isinstance(final, dict) or final.get("type") != "result":
            raise ValueError("Qwen Code JSON stream has no final result event")
        content = str(final.get("result", ""))
        if final.get("is_error") or content.startswith("[API Error:"):
            raise OSError(content or "Qwen Code reported an error")
        if not content and final.get("structured_result") is None:
            raise OSError("Qwen Code returned no terminal content")
        structured = final.get("structured_result")
        if structured is not None and not isinstance(structured, dict):
            raise ValueError("Qwen Code structured result is not an object")
        usage = final.get("usage", {})
        return ModelResponse(
            content=content,
            structured=structured,
            usage=ModelUsage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
            ),
            model=identity,
            prompt_hash=prompt_hash,
            response_hash=hashlib.sha256(content.encode()).hexdigest(),
            started_at=started,
            ended_at=ended,
            seed=seed,
        )

    def tool_calls(self) -> list[str]:
        calls: list[str] = []
        for event in self.last_events:
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    calls.append(str(item.get("name", "")))
        return calls

    def _path_env(self) -> str:
        if self.node_dir is None:
            raise FileNotFoundError("node")
        return self.node_dir + os.pathsep + os.environ.get("PATH", "")
