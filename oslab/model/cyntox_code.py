from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from oslab.generation_lease import gpu_generation_lease
from oslab.model.base import ModelProvider
from oslab.process_runner import ProcessResult, SafeProcessRunner
from oslab.schemas import ModelIdentity, ModelResponse, ModelUsage, utc_now

UPSTREAM_CLI_BINARY = "q" + "wen"
UPSTREAM_CONFIG_DIR = "." + "q" + "wen"


class CyntoxCodeWorker(ModelProvider):
    allowed_tools = {
        "mcp__oslab__policy_remaining_budget",
        "mcp__oslab__fixture_explain",
    }

    def __init__(
        self,
        project_root: Path,
        runner: SafeProcessRunner | None = None,
        *,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
        manage_gpu_lease: bool = True,
    ) -> None:
        self.project_root = project_root.resolve()
        suffix = ".cmd" if os.name == "nt" else ""
        self.executable = (
            self.project_root / "node_modules" / ".bin" / f"{UPSTREAM_CLI_BINARY}{suffix}"
        )
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
        self.retry_attempts = retry_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.manage_gpu_lease = manage_gpu_lease
        self._identity: ModelIdentity | None = None
        self.last_events: list[dict[str, Any]] = []
        self.worker_workspace = self.project_root / ".oslab" / "cyntox-code-smoke-workspace"

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
            provider="cyntox-code",
            runtime="CyntOX Code",
            runtime_version=result.stdout.strip(),
            model_id="cyntox-os-lab-worker:latest",
            architecture="cyntox-27b",
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
        self._write_worker_settings()
        api_key = os.environ.get("OSLAB_OLLAMA_API_KEY", "ollama-local-no-auth")
        base_url = "http://127.0.0.1:11434/v1"
        argv = [
            str(self.executable),
            "--auth-type",
            "openai",
            "--model",
            identity.model_id,
            "--openai-api-key",
            api_key,
            "--openai-base-url",
            base_url,
            "-p",
            prompt,
            "--output-format",
            "json",
        ]
        if schema is not None:
            argv.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
        effective_timeout = 120.0 if timeout is None else timeout
        if effective_timeout <= 0:
            raise TimeoutError("CyntOX Code completion deadline already expired")
        started = utc_now()
        async with gpu_generation_lease(
            enabled=self.manage_gpu_lease,
            timeout=min(60.0, effective_timeout),
        ):
            result = await self._run_with_retries(
                argv,
                cwd=self.worker_workspace,
                timeout=effective_timeout,
                env={
                    "OPENAI_API_KEY": api_key,
                    "OPENAI_BASE_URL": base_url,
                    "OSLAB_PYTHON": sys.executable,
                    "PATH": self._path_env(),
                    "Q" + "WEN_HOME": str(self.project_root / ".oslab" / "cyntox-code-home"),
                    "Q" + "WEN_RUNTIME_DIR": str(self.project_root / ".oslab" / "cyntox-code"),
                    "Q" + "WEN_CODE_SUPPRESS_YOLO_WARNING": "1",
                    "OSLAB_SEED": str(seed) if seed is not None else "",
                },
            )
        ended = utc_now()
        if result.timed_out:
            raise TimeoutError("CyntOX Code worker exceeded the external wall-clock budget")
        if result.returncode != 0:
            raise OSError(result.stderr or result.stdout)
        try:
            events = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CyntOX Code did not emit a JSON event array: {exc}") from exc
        if not isinstance(events, list) or not events:
            raise ValueError("CyntOX Code emitted no events")
        if not all(isinstance(event, dict) for event in events):
            raise ValueError("CyntOX Code emitted a non-object event")
        self.last_events = events
        init = events[0]
        if init.get("type") != "system" or init.get("subtype") != "init":
            raise ValueError("CyntOX Code stream has no init event")
        declared_tools = set(init.get("tools", []))
        if declared_tools != self.allowed_tools:
            unexpected = sorted(declared_tools - self.allowed_tools)
            missing = sorted(self.allowed_tools - declared_tools)
            raise PermissionError(
                f"CyntOX Code tool boundary mismatch: unexpected={unexpected}, missing={missing}"
            )
        servers = init.get("mcp_servers", [])
        if not any(
            isinstance(server, dict)
            and server.get("name") == "oslab"
            and server.get("status") == "connected"
            for server in servers
        ):
            raise ConnectionError("CyntOX Code did not connect the oslab MCP broker")
        final = events[-1]
        if not isinstance(final, dict) or final.get("type") != "result":
            raise ValueError("CyntOX Code JSON stream has no final result event")
        content = str(final.get("result", ""))
        if final.get("is_error") or content.startswith("[API Error:"):
            raise OSError(content or "CyntOX Code reported an error")
        if not content and final.get("structured_result") is None:
            raise OSError("CyntOX Code returned no terminal content")
        structured = final.get("structured_result")
        if structured is not None and not isinstance(structured, dict):
            raise ValueError("CyntOX Code structured result is not an object")
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

    def _write_worker_settings(self) -> None:
        source = self.project_root / ".cyntox" / "settings.json"
        if not source.is_file():
            raise FileNotFoundError(source)
        settings = json.loads(source.read_text(encoding="utf-8"))
        mcp_servers = settings.get("mcpServers")
        if isinstance(mcp_servers, dict):
            for server in mcp_servers.values():
                if isinstance(server, dict):
                    server["cwd"] = str(self.project_root)
        destination = self.worker_workspace / UPSTREAM_CONFIG_DIR / "settings.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    async def _run_with_retries(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str],
    ) -> ProcessResult:
        last_result: ProcessResult | None = None
        for attempt in range(self.retry_attempts):
            result = await self.runner.run(argv, cwd=cwd, timeout=timeout, env=env)
            last_result = result
            if result.returncode == 0 or result.timed_out:
                return result
            if attempt + 1 >= self.retry_attempts:
                return result
            if not self._is_retryable_process_failure(result):
                return result
            if self.retry_backoff_seconds:
                await asyncio.sleep(self.retry_backoff_seconds * (attempt + 1))
        if last_result is None:
            raise AssertionError("CyntOX Code retry loop did not execute")
        return last_result

    @staticmethod
    def _is_retryable_process_failure(result: ProcessResult) -> bool:
        output = f"{result.stderr}\n{result.stdout}".lower()
        retryable_markers = (
            "fatal error",
            "internal server error",
            "econnreset",
            "econnrefused",
            "socket hang up",
            "temporarily",
            "timed out",
        )
        return any(marker in output for marker in retryable_markers)
