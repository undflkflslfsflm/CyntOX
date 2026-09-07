from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import http.client
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import jsonschema
import psutil

from oslab.airllm_protocol import InvalidAirLlmResponse, sanitize_response
from oslab.airllm_runtime import (
    manifest_hash,
    runtime_home,
    runtime_python,
    status,
    worker_environment,
)
from oslab.generation_lease import gpu_generation_lease
from oslab.gpu_lease import selected_gpu_uuid
from oslab.model.base import ModelProvider
from oslab.model_registry import allowed_qwythos_completion_tokens, locked_model
from oslab.resource_lease import AirLlmAdmissionLease
from oslab.schemas import ModelIdentity, ModelResponse, ModelUsage

MAX_WORKER_REQUEST_BYTES = 4 * 1024 * 1024
MAX_WORKER_RESPONSE_BYTES = 4 * 1024 * 1024
RESIDENT_CONTEXT_LIMIT = 8192
MAX_STARTUP_HANDSHAKE_CHARS = 64 * 1024
_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")
_WORKER_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
SAFE_WORKER_VALIDATION_MESSAGES = frozenset(
    {
        "nested reasoning span",
        "misordered reasoning span",
        "malformed reasoning tag",
        "incomplete reasoning span",
        "empty final response",
        "repetition loop",
    }
)


def render_airllm_prompt(
    messages: list[dict[str, str]], schema: dict[str, Any] | None = None
) -> str:
    """Render the exact text sent to the isolated AirLLM worker."""
    prompt = "\n\n".join(
        f"{item.get('role', 'user')}: {item.get('content', '')}" for item in messages
    )
    if schema is not None:
        prompt += "\n\nReturn only JSON matching this schema:\n" + json.dumps(
            schema, sort_keys=True
        )
    return prompt


class AirLlmProtocolError(RuntimeError):
    """The authenticated worker violated its local protocol."""


class AirLlmWorkerError(RuntimeError):
    """The worker completed an HTTP exchange but rejected the operation."""

    def __init__(self, status_code: int, code: str, detail: str = "") -> None:
        self.status_code = status_code
        self.code = code
        suffix = f": {detail}" if detail else ""
        super().__init__(f"AirLLM worker error {status_code} ({code}){suffix}")


class AirLlmTransportError(RuntimeError):
    """The worker connection failed or exceeded its deadline."""


class AirLlmProvider(ModelProvider):
    def __init__(
        self,
        root: Path,
        *,
        worker_python: Path | None = None,
        fake: bool = False,
        max_new_tokens: int = 2048,
        backend: str = "airllm",
        manage_gpu_lease: bool = True,
        qualification_mode: bool = False,
    ) -> None:
        if backend not in {"airllm", "resident"}:
            raise ValueError(f"unsupported Qwythos backend: {backend}")
        self.root = root
        self.worker_python = worker_python or runtime_python()
        self.fake = fake
        self.max_new_tokens = max(1, min(max_new_tokens, 2048))
        self.backend = backend
        self.manage_gpu_lease = manage_gpu_lease
        self.qualification_mode = qualification_mode
        self.process: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self.token: str | None = None
        self.worker_pid: int | None = None
        self._job_handle: int | None = None
        self._process_group_id: int | None = None
        self._identity: dict[str, Any] = {}
        self._expected_gpu_uuid: str | None = None
        self._diagnostics: deque[str] = deque(maxlen=100)
        self._drain_threads: list[threading.Thread] = []
        self._resource_admission: AirLlmAdmissionLease | None = None
        self._resource_admission_lock = threading.Lock()
        self.last_metadata: dict[str, Any] = {}

    def _acquire_resource_admission_sync(self) -> bool:
        if not self.manage_gpu_lease:
            return False
        with self._resource_admission_lock:
            if self._resource_admission is not None and self._resource_admission.acquired:
                return False
            self._resource_admission = None
            admission = AirLlmAdmissionLease(self.root)
            admission.acquire()
            self._resource_admission = admission
            return True

    async def _acquire_resource_admission(self) -> bool:
        task = asyncio.create_task(asyncio.to_thread(self._acquire_resource_admission_sync))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            newly_acquired = task.result()
            if newly_acquired:
                await asyncio.to_thread(self._release_resource_admission_sync)
            raise

    def _release_resource_admission_sync(self) -> bool:
        if not self.manage_gpu_lease:
            return True
        with self._resource_admission_lock:
            admission = self._resource_admission
            if admission is None:
                return True
            admission.release()
            if admission.acquired:
                return False
            self._resource_admission = None
            return True

    async def start(self, *, timeout: float | None = None) -> None:
        total_budget = 900.0 if timeout is None else timeout
        if total_budget <= 0:
            raise TimeoutError("Qwythos startup deadline already expired")
        budget_started = time.monotonic()
        newly_admitted = await self._acquire_resource_admission()
        try:
            if self.process and self.process.poll() is None:
                return
            lease_budget = total_budget - (time.monotonic() - budget_started)
            if lease_budget <= 0:
                raise TimeoutError("Qwythos startup deadline expired during resource admission")
            async with gpu_generation_lease(
                enabled=self.manage_gpu_lease,
                timeout=min(60.0, lease_budget),
            ):
                remaining = total_budget - (time.monotonic() - budget_started)
                if remaining <= 0:
                    raise TimeoutError("Qwythos startup deadline expired waiting for the GPU")
                await self._start_unlocked(timeout=remaining)
        except BaseException:
            if newly_admitted and (self.process is None or self.process.poll() is not None):
                await asyncio.to_thread(self._release_resource_admission_sync)
            raise

    async def _start_unlocked(self, *, timeout: float | None = None) -> None:
        timeout = 900.0 if timeout is None else timeout
        deadline = time.monotonic() + timeout
        if self.process and self.process.poll() is None:
            return
        if self.process is not None:
            # A replacement worker stays inside the already-held resource admission;
            # releasing it here would reopen a build/QEMU/fuzz TOCTOU while the caller
            # still owns the GPU generation lease.
            await self._close_impl(force=True, release_admission=False)
        if not self.worker_python.is_file():
            raise RuntimeError("AirLLM runtime is not set up; run `cyntox model airllm setup`")
        env = worker_environment(self.root)
        expected_gpu_uuid = env.get("CYNTOX_GPU_UUID") or selected_gpu_uuid()
        if not expected_gpu_uuid:
            if not self.fake:
                raise RuntimeError("could not identify the physical GPU selected for Qwythos")
            expected_gpu_uuid = "unknown-gpu"
        env["CYNTOX_GPU_UUID"] = expected_gpu_uuid
        self._expected_gpu_uuid = expected_gpu_uuid
        if self.fake:
            env["CYNTOX_AIRLLM_FAKE"] = "1"
            for name in ("CYNTOX_AIRLLM_FAKE_MODE", "CYNTOX_AIRLLM_FAKE_DELAY"):
                if name in os.environ:
                    env[name] = os.environ[name]
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            work_dir = runtime_home()
            work_dir.mkdir(parents=True, exist_ok=True)
            command = [
                str(self.worker_python),
                str(self.root / "scripts" / "cyntox_airllm_worker.py"),
                "serve",
                "--backend",
                self.backend,
            ]
            if self.qualification_mode:
                command.append("--qualification-mode")
            self.process = subprocess.Popen(  # noqa: ASYNC220,S603
                command,
                cwd=work_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
            if os.name != "nt":
                self._process_group_id = self.process.pid
            if os.name == "nt":
                self._attach_windows_job(self.process)
            assert self.process.stdout is not None
            self._start_stream_drain(self.process.stderr)
            handshake_timeout = deadline - time.monotonic()
            if handshake_timeout <= 0:
                raise TimeoutError("Qwythos startup deadline expired before handshake")
            line = await asyncio.wait_for(
                asyncio.to_thread(
                    self.process.stdout.readline,
                    MAX_STARTUP_HANDSHAKE_CHARS + 1,
                ),
                handshake_timeout,
            )
            if not line:
                await asyncio.sleep(0.1)
                detail = self._diagnostics[-1] if self._diagnostics else "no diagnostics"
                raise RuntimeError(
                    "Qwythos worker exited before startup handshake "
                    f"(code={self.process.poll()}, detail={detail})"
                )
            if len(line) > MAX_STARTUP_HANDSHAKE_CHARS or not line.endswith("\n"):
                raise AirLlmProtocolError("Qwythos worker startup handshake exceeded its limit")
            try:
                handshake = json.loads(line)
            except json.JSONDecodeError as error:
                raise AirLlmProtocolError(
                    "Qwythos worker startup handshake was not valid JSON"
                ) from error
            if not isinstance(handshake, dict):
                raise AirLlmProtocolError("Qwythos worker startup handshake was not an object")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Qwythos startup deadline expired before identity validation")
            await asyncio.wait_for(
                asyncio.to_thread(self._validate_identity, handshake, handshake=True),
                remaining,
            )
            self.worker_pid = int(handshake["pid"])
            self.port = int(handshake["port"])
            self.token = str(handshake["token"])
            self._identity = {
                name: value for name, value in handshake.items() if name not in {"port", "token"}
            }
            self._start_output_drains()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Qwythos startup deadline expired before health probe")
            await self._probe_with_timeout(remaining)
            if time.monotonic() > deadline:
                raise TimeoutError("Qwythos startup response crossed its absolute deadline")
        except BaseException:
            await self.close(force=True)
            raise

    def _start_stream_drain(self, stream: Any) -> None:
        if stream is None:
            return

        def drain() -> None:
            for line in stream:
                cleaned = line.strip()
                if cleaned:
                    self._diagnostics.append(cleaned[:500])

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        self._drain_threads.append(thread)

    def _start_output_drains(self) -> None:
        if self.process is not None:
            self._start_stream_drain(self.process.stdout)

    def _validate_identity(self, body: dict[str, Any], *, handshake: bool) -> None:
        model = locked_model(self.root, "qwythos-airllm")
        expected_backend = (
            "fake"
            if self.fake
            else ("airllm" if self.backend == "airllm" else "transformers-resident")
        )
        expected_kind = "fake" if self.fake else "real"
        expected_context = (
            model.context_limit if self.fake or self.backend == "airllm" else RESIDENT_CONTEXT_LIMIT
        )
        expected_implementation = (
            "fake"
            if self.fake
            else (
                "airllm.airllm_qwen3_5.AirLLMQwen3_5"
                if self.backend == "airllm"
                else "transformers.models.qwen3_5.modeling_qwen3_5.Qwen3_5ForCausalLM"
            )
        )
        failures: list[str] = []
        reported_pid = body.get("pid")
        valid_handshake_pids: set[int] = set()
        if self.process is not None:
            valid_handshake_pids.add(self.process.pid)
            with contextlib.suppress(psutil.Error):
                valid_handshake_pids.update(
                    child.pid for child in psutil.Process(self.process.pid).children(recursive=True)
                )
        pid_matches = (
            isinstance(reported_pid, int)
            and not isinstance(reported_pid, bool)
            and (
                reported_pid in valid_handshake_pids
                if handshake
                else reported_pid == self.worker_pid
            )
        )
        checks = {
            "protocol": body.get("protocol") == 1 if handshake else True,
            "pid": isinstance(reported_pid, int)
            and not isinstance(reported_pid, bool)
            and pid_matches,
            "ok": True if handshake else body.get("ok") is True,
            "model_id": body.get("model_id") == model.model_id,
            "revision": body.get("revision") == model.revision,
            "architecture": body.get("architecture") == "Qwen3_5ForConditionalGeneration",
            "precision": body.get("precision") == "bf16",
            "context_limit": body.get("context_limit") == expected_context,
            "gpu_uuid": body.get("gpu_uuid") == self._expected_gpu_uuid,
            "startup_peak_vram_mib": (
                isinstance(body.get("startup_peak_vram_mib"), int | float)
                and not isinstance(body.get("startup_peak_vram_mib"), bool)
                and math.isfinite(float(body["startup_peak_vram_mib"]))
                and float(body["startup_peak_vram_mib"]) >= 0
            ),
            "backend_kind": body.get("backend_kind") == expected_kind,
            "backend_name": body.get("backend_name") == expected_backend,
            "implementation_class": body.get("implementation_class") == expected_implementation,
            "qualification_mode": body.get("qualification_mode") is self.qualification_mode,
            "offline_environment": body.get("offline_environment") is True,
            "session_id": isinstance(body.get("session_id"), str)
            and _SESSION_ID_RE.fullmatch(str(body["session_id"])) is not None,
        }
        if not self.fake:
            prepared = status(self.root)
            manifest = prepared.get("manifest")
            checks.update(
                {
                    "runtime_ready": prepared.get("runtime_ready") is True,
                    "snapshot_ready": prepared.get("snapshot_ready") is True,
                    "shards_ready": prepared.get("shards_ready") is True,
                    "offline_reload_proven": prepared.get("offline_reload_proven") is True,
                    "resident_ready": self.backend != "resident"
                    or prepared.get("resident_ready") is True,
                    "optional_kernels_absent": prepared.get("native_optional_kernels") is False,
                    "runtime_lock": isinstance(manifest, dict)
                    and body.get("runtime_lock_sha256") == manifest.get("runtime_lock_sha256"),
                    "snapshot_manifest": body.get("snapshot_manifest_sha256")
                    == manifest_hash(Path(prepared["home"]) / "snapshot-manifest.json"),
                    "shard_manifest": body.get("shard_manifest_sha256")
                    == manifest_hash(Path(prepared["home"]) / "shard-manifest.json"),
                }
            )
        if handshake:
            checks.update(
                {
                    "port": isinstance(body.get("port"), int) and 1 <= int(body["port"]) <= 65535,
                    "token": isinstance(body.get("token"), str)
                    and _WORKER_TOKEN_RE.fullmatch(str(body["token"])) is not None,
                }
            )
        for name, passed in checks.items():
            if not passed:
                failures.append(name)
        if failures:
            raise RuntimeError(f"Qwythos worker identity mismatch: {', '.join(failures)}")

    def _attach_windows_job(self, process: subprocess.Popen[str]) -> None:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("CreateJobObjectW failed")

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class Io(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ) or not kernel32.AssignProcessToJobObject(
            handle, ctypes.c_void_p(int(cast(Any, process)._handle))
        ):
            kernel32.CloseHandle(handle)
            raise OSError("failed to configure AirLLM kill-on-close Job Object")
        self._job_handle = int(handle)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        if self.port is None or self.token is None:
            raise RuntimeError("AirLLM worker has not started")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        if data is not None and len(data) > MAX_WORKER_REQUEST_BYTES:
            raise ValueError(f"Qwythos worker request exceeds {MAX_WORKER_REQUEST_BYTES} bytes")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=timeout) as response:  # noqa: S310
                raw_body = self._read_bounded_body(response)
        except urllib.error.HTTPError as error:
            raw_body = self._read_bounded_body(error)
            detail = ""
            code = "http_error"
            with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                decoded_error = json.loads(raw_body.decode("utf-8"))
                if isinstance(decoded_error, dict) and isinstance(decoded_error.get("error"), str):
                    code = str(decoded_error["error"])
                    message = decoded_error.get("message")
                    if isinstance(message, str):
                        detail = (
                            "CUDA out of memory"
                            if "out of memory" in message.casefold()
                            else (
                                message[:500] if message in SAFE_WORKER_VALIDATION_MESSAGES else ""
                            )
                        )
            raise AirLlmWorkerError(error.code, code, detail) from error
        except (
            TimeoutError,
            urllib.error.URLError,
            http.client.HTTPException,
            ConnectionError,
            OSError,
        ) as error:
            raise AirLlmTransportError(
                f"Qwythos worker transport failed: {type(error).__name__}"
            ) from error
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AirLlmProtocolError("Qwythos worker returned invalid JSON") from error
        if not isinstance(body, dict):
            raise AirLlmProtocolError("Qwythos worker returned a non-object response")
        return body

    @staticmethod
    def _read_bounded_body(response: Any) -> bytes:
        try:
            declared = int(response.headers.get("Content-Length", ""))
        except (TypeError, ValueError) as error:
            raise AirLlmProtocolError(
                "Qwythos worker response omitted a valid Content-Length"
            ) from error
        if declared <= 0 or declared > MAX_WORKER_RESPONSE_BYTES:
            raise AirLlmProtocolError("Qwythos worker returned an invalid response size")
        body = response.read(declared + 1)
        if len(body) != declared:
            raise AirLlmProtocolError("Qwythos worker response length did not match its header")
        return bytes(body)

    async def _probe_with_timeout(self, timeout: float) -> ModelIdentity:
        started = time.monotonic()
        try:
            body = await asyncio.wait_for(
                asyncio.to_thread(self._request, "GET", "/health", None, timeout),
                timeout,
            )
        except TimeoutError as error:
            raise AirLlmTransportError("Qwythos health probe exceeded its deadline") from error
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise AirLlmTransportError("Qwythos health probe crossed its absolute deadline")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._validate_identity, body, handshake=False),
                remaining,
            )
        except TimeoutError as error:
            raise AirLlmTransportError(
                "Qwythos health identity validation exceeded its deadline"
            ) from error
        if time.monotonic() - started > timeout:
            raise AirLlmTransportError("Qwythos health probe crossed its absolute deadline")
        if self._identity and body.get("session_id") != self._identity.get("session_id"):
            raise RuntimeError("Qwythos worker session changed unexpectedly")
        model = locked_model(self.root, "qwythos-airllm")
        return ModelIdentity(
            provider="qwythos",
            runtime=str(body["backend_name"]),
            runtime_version=("3.3.0" if self.backend == "airllm" else "transformers-5.12.1"),
            model_id=model.model_id,
            architecture=str(body["architecture"]),
            quantization="bf16",
            format="safetensors",
            context_limit=int(body["context_limit"]),
            endpoint=f"http://127.0.0.1:{self.port}",
            capabilities=["text", "reasoning"],
        )

    async def probe(self) -> ModelIdentity:
        return await self._probe_with_timeout(5.0)

    async def qualification_fault(
        self,
        fault: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Exercise a fixed worker fault only in an explicit qualification process."""
        if not self.qualification_mode:
            raise RuntimeError("qualification fault probes are disabled")
        if fault not in {"crash", "timeout", "malformed", "oom", "network"}:
            raise ValueError(f"unsupported qualification fault: {fault}")
        if self.process is None or self.process.poll() is not None:
            await self.start(timeout=min(900.0, max(timeout, 0.001)))
        body = await asyncio.to_thread(
            self._request,
            "POST",
            "/qualification/fault",
            {"fault": fault},
            timeout,
        )
        if body.get("session_id") != self._identity.get("session_id"):
            raise AirLlmProtocolError("qualification result came from an unexpected worker session")
        if fault == "malformed":
            text = body.get("text")
            if not isinstance(text, str):
                raise AirLlmProtocolError("malformed-output probe omitted text")
            sanitize_response(text)
            raise AirLlmProtocolError("malformed-output probe was unexpectedly accepted")
        return body

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        timeout: float | None = None,
    ) -> ModelResponse:
        total_budget = 900.0 if timeout is None else timeout
        if total_budget <= 0:
            raise TimeoutError("Qwythos completion deadline already expired")
        budget_started = time.monotonic()
        newly_admitted = await self._acquire_resource_admission()
        try:
            lease_budget = total_budget - (time.monotonic() - budget_started)
            if lease_budget <= 0:
                raise TimeoutError("Qwythos completion deadline expired during resource admission")
            async with gpu_generation_lease(
                enabled=self.manage_gpu_lease,
                timeout=min(60.0, lease_budget),
            ):
                remaining = total_budget - (time.monotonic() - budget_started)
                if remaining <= 0:
                    raise TimeoutError("Qwythos completion deadline expired waiting for the GPU")
                return await self._complete_unlocked(
                    messages,
                    schema=schema,
                    seed=seed,
                    timeout=remaining,
                )
        except BaseException:
            if newly_admitted and (self.process is None or self.process.poll() is not None):
                await asyncio.to_thread(self._release_resource_admission_sync)
            raise

    async def _complete_unlocked(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        seed: int | None = None,
        timeout: float | None = None,
    ) -> ModelResponse:
        total_budget = 900.0 if timeout is None else timeout
        budget_started = time.monotonic()
        if self.process is None or self.process.poll() is not None:
            await self._start_unlocked(timeout=min(total_budget, 900.0))
        prompt = render_airllm_prompt(messages, schema)
        started = datetime.now(UTC)
        request_timeout = total_budget - (time.monotonic() - budget_started)
        if request_timeout <= 0:
            raise TimeoutError("Qwythos completion deadline expired before generation")
        try:
            body = await asyncio.wait_for(
                asyncio.to_thread(
                    self._request,
                    "POST",
                    "/generate",
                    {
                        "prompt": prompt,
                        "max_new_tokens": self.max_new_tokens,
                        "seed": seed,
                        "structured": schema is not None,
                    },
                    request_timeout,
                ),
                request_timeout,
            )
        except asyncio.CancelledError:
            await self.close(force=True)
            raise
        except (AirLlmProtocolError, AirLlmTransportError):
            await self.close(force=True)
            raise
        except TimeoutError as error:
            await self.close(force=True)
            raise AirLlmTransportError("Qwythos worker transport failed: TimeoutError") from error
        if time.monotonic() - budget_started > total_budget:
            await self.close(force=True)
            raise AirLlmTransportError("Qwythos generation response crossed its absolute deadline")
        if body.get("session_id") != self._identity.get("session_id"):
            await self.close(force=True)
            raise AirLlmProtocolError("Qwythos generation came from an unexpected worker session")
        text = body.get("text")
        if not isinstance(text, str):
            await self.close(force=True)
            raise AirLlmProtocolError("Qwythos generation text was not a string")
        prompt_tokens = body.get("prompt_tokens")
        completion_tokens = body.get("completion_tokens")
        elapsed_seconds = body.get("elapsed_seconds")
        peak_vram_mib = body.get("peak_vram_mib")
        startup_peak_vram_mib = self._identity.get("startup_peak_vram_mib")
        if (
            not isinstance(prompt_tokens, int)
            or isinstance(prompt_tokens, bool)
            or prompt_tokens <= 0
            or not isinstance(completion_tokens, int)
            or isinstance(completion_tokens, bool)
            or not 0 < completion_tokens <= allowed_qwythos_completion_tokens(self.max_new_tokens)
            or not isinstance(elapsed_seconds, int | float)
            or isinstance(elapsed_seconds, bool)
            or not math.isfinite(float(elapsed_seconds))
            or float(elapsed_seconds) < 0
            or not isinstance(peak_vram_mib, int | float)
            or isinstance(peak_vram_mib, bool)
            or not math.isfinite(float(peak_vram_mib))
            or float(peak_vram_mib) < 0
            or not isinstance(startup_peak_vram_mib, int | float)
            or isinstance(startup_peak_vram_mib, bool)
            or float(peak_vram_mib) < float(startup_peak_vram_mib)
        ):
            await self.close(force=True)
            raise AirLlmProtocolError("Qwythos generation metadata was invalid")
        try:
            content = sanitize_response(text)
        except InvalidAirLlmResponse:
            await self.close(force=True)
            raise
        ended = datetime.now(UTC)
        structured = None
        if schema is not None:
            try:
                candidate = json.loads(content)
                structured = candidate if isinstance(candidate, dict) else None
            except json.JSONDecodeError:
                structured = None
            if structured is None:
                await self.close(force=True)
                raise ValueError("AirLLM response did not contain the requested JSON object")
            try:
                jsonschema.validate(instance=structured, schema=schema)
            except jsonschema.ValidationError:
                await self.close(force=True)
                raise
        if time.monotonic() - budget_started > total_budget:
            await self.close(force=True)
            raise AirLlmTransportError("Qwythos response validation crossed its absolute deadline")
        self.last_metadata = {
            "elapsed_seconds": float(elapsed_seconds),
            "peak_vram_mib": float(peak_vram_mib),
            "backend_name": str(body.get("backend_name", self.backend)),
            "backend_kind": str(self._identity.get("backend_kind", "unknown")),
            "backend_class": str(self._identity.get("backend_class", "")),
            "implementation_class": str(self._identity.get("implementation_class", "")),
            "session_id": str(self._identity.get("session_id", "")),
            "worker_pid": self.worker_pid,
            "context_limit": self._identity.get("context_limit"),
            "max_new_tokens": self.max_new_tokens,
            "gpu_uuid": self._identity.get("gpu_uuid"),
            "startup_peak_vram_mib": self._identity.get("startup_peak_vram_mib"),
            "runtime_lock_sha256": self._identity.get("runtime_lock_sha256"),
            "snapshot_manifest_sha256": self._identity.get("snapshot_manifest_sha256"),
            "shard_manifest_sha256": self._identity.get("shard_manifest_sha256"),
        }
        identity = ModelIdentity(
            provider="qwythos",
            runtime=self.last_metadata["backend_name"],
            runtime_version="3.3.0" if self.backend == "airllm" else "transformers-5.12.1",
            model_id=locked_model(self.root, "qwythos-airllm").model_id,
            architecture="Qwen3_5ForConditionalGeneration",
            quantization="bf16",
            format="safetensors",
            context_limit=int(self._identity["context_limit"]),
            endpoint=f"http://127.0.0.1:{self.port}",
            capabilities=["text", "reasoning"],
        )
        return ModelResponse(
            content=content,
            structured=structured,
            usage=ModelUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                eval_seconds=self.last_metadata["elapsed_seconds"],
            ),
            model=identity,
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            response_hash=hashlib.sha256(content.encode()).hexdigest(),
            started_at=started,
            ended_at=ended,
            seed=seed,
        )

    async def close(self, *, force: bool = False) -> None:
        cleanup = asyncio.create_task(self._close_impl(force=force, release_admission=True))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            while not cleanup.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            raise

    async def _close_impl(
        self,
        *,
        force: bool = False,
        release_admission: bool = True,
    ) -> None:
        process = self.process
        if not process:
            if release_admission:
                released = await asyncio.to_thread(self._release_resource_admission_sync)
                if not released:
                    raise RuntimeError("AirLLM resource admission cleanup was not verified")
            return
        process_group_id = self._process_group_id
        descendants: list[psutil.Process] = []
        with contextlib.suppress(psutil.Error):
            descendants = psutil.Process(process.pid).children(recursive=True)
        try:
            if process.poll() is None and not force:
                try:
                    await asyncio.to_thread(self._request, "POST", "/shutdown", {}, 3.0)
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 5.0)
                except Exception:
                    force = True
            if process.poll() is None:
                if os.name != "nt":
                    with contextlib.suppress(ProcessLookupError):
                        cast(Any, os).killpg(
                            process_group_id or process.pid,
                            cast(Any, signal).SIGKILL,
                        )
                else:
                    with contextlib.suppress(OSError):
                        process.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 10.0)
            if os.name != "nt" and process_group_id is not None:
                with contextlib.suppress(ProcessLookupError):
                    cast(Any, os).killpg(process_group_id, cast(Any, signal).SIGKILL)
        finally:
            if self._job_handle and os.name == "nt":
                ctypes.windll.kernel32.CloseHandle(self._job_handle)
            await asyncio.to_thread(self._reap_descendants, descendants)
            self.process = None
            self.port = None
            self.token = None
            self.worker_pid = None
            self._job_handle = None
            self._process_group_id = None
            self._identity = {}
            self._expected_gpu_uuid = None
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            for thread in self._drain_threads:
                thread.join(timeout=1)
            self._drain_threads = []
            if release_admission:
                released = await asyncio.to_thread(self._release_resource_admission_sync)
                if not released:
                    raise RuntimeError("AirLLM resource admission cleanup was not verified")

    @staticmethod
    def _reap_descendants(descendants: list[psutil.Process]) -> None:
        _, alive = psutil.wait_procs(descendants, timeout=2)
        for child in alive:
            with contextlib.suppress(psutil.Error):
                child.kill()
        psutil.wait_procs(alive, timeout=3)

    async def __aenter__(self) -> AirLlmProvider:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
