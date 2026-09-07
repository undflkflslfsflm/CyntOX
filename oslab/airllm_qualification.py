from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ipaddress
import json
import math
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from oslab import airllm_runtime
from oslab.gpu_lease import selected_gpu_uuid
from oslab.model.airllm import AirLlmProvider

ProviderFactory = Callable[..., AirLlmProvider]
FallbackRunner = Callable[[Path], bool]
ResourceCleanup = Callable[[Path, float | None, bool], dict[str, Any]]
_FALLBACK_MODEL = "cyntox:latest"
_RESOURCE_CLEANUP_TIMEOUT_SECONDS = 30.0
_FAULT_EXPECTATIONS = {
    "crash": "worker_failure",
    "timeout": "timeout",
    "malformed": "invalid_response",
    "oom": "oom",
}


def _expected_backend(backend: str) -> str:
    return "transformers-resident" if backend == "resident" else "airllm"


def _new_provider(factory: ProviderFactory, root: Path, backend: str) -> AirLlmProvider:
    return factory(
        root,
        backend=backend,
        max_new_tokens=1,
        manage_gpu_lease=False,
        qualification_mode=True,
    )


def _provider_pids(provider: AirLlmProvider) -> set[int]:
    pids: set[int] = set()
    if provider.process is not None:
        pids.add(provider.process.pid)
        with contextlib.suppress(psutil.Error):
            pids.update(
                child.pid for child in psutil.Process(provider.process.pid).children(recursive=True)
            )
    if provider.worker_pid is not None:
        pids.add(provider.worker_pid)
    return pids


def _windows_process_names() -> list[tuple[int, str]] | None:
    """Take an access-independent process-name snapshot on Windows."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not handle or int(handle) == invalid_handle:
        return None
    entries: list[tuple[int, str]] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        present = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        if not present:
            return None
        while present:
            entries.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            present = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return entries


def _ollama_runner_pids() -> list[int] | None:
    """Return model-runner PIDs, or None when the inventory is not attestable."""
    pids: list[int] = []
    if os.name == "nt":
        windows_processes = _windows_process_names()
        if windows_processes is None:
            return None
        for pid, raw_name in windows_processes:
            name = raw_name.strip().lower()
            if not name:
                return None
            if "ollama_llama_server" in name:
                pids.append(pid)
                continue
            if name not in {"ollama", "ollama.exe"}:
                continue
            try:
                command = " ".join(psutil.Process(pid).cmdline()).lower()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, TypeError):
                return None
            if " runner " in f" {command} ":
                pids.append(pid)
        return sorted(set(pids))

    try:
        processes = psutil.process_iter(["pid", "name", "cmdline"])
    except psutil.Error:
        return None
    try:
        for process in processes:
            try:
                candidate_pid = process.info.get("pid")
                candidate_name = process.info.get("name")
                if candidate_pid == 0:
                    # PID 0 is a synthetic kernel/idle process, never an Ollama runner.
                    continue
                if (
                    not isinstance(candidate_pid, int)
                    or isinstance(candidate_pid, bool)
                    or candidate_pid < 0
                    or not isinstance(candidate_name, str)
                    or not candidate_name.strip()
                ):
                    return None
                name = candidate_name.lower()
                raw_command = process.info.get("cmdline")
                if name in {"ollama", "ollama.exe"} and not isinstance(raw_command, list):
                    return None
                command = " ".join(str(item) for item in (raw_command or [])).lower()
            except psutil.NoSuchProcess:
                # A process that disappeared while enumerating cannot still own GPU state.
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess, TypeError):
                return None
            if "ollama_llama_server" in name or (
                name in {"ollama", "ollama.exe"} and " runner " in f" {command} "
            ):
                pids.append(candidate_pid)
    except psutil.Error:
        return None
    return sorted(set(pids))


def _ollama_loaded_models(base_url: str, *, timeout: float) -> list[str] | None:
    """Read Ollama's resident model inventory; None means it could not be attested."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - caller validates a loopback URL
            f"{base_url}/api/ps",
            timeout=max(0.001, timeout),
        ) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("models"), list):
        return None
    names: list[str] = []
    for model in decoded["models"]:
        if not isinstance(model, dict):
            return None
        name = model.get("name") or model.get("model")
        if not isinstance(name, str) or not name.strip():
            return None
        names.append(name.strip())
    return sorted(set(names))


def _request_ollama_unload(base_url: str, *, timeout: float) -> bool:
    payload = json.dumps({"model": _FALLBACK_MODEL, "keep_alive": 0}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - caller validates a loopback URL
        f"{base_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - caller validates a loopback URL
            request,
            timeout=max(0.001, timeout),
        ):
            return True
    except (OSError, urllib.error.URLError):
        return False


def _default_fallback_cleanup(
    root: Path,
    baseline_vram_mib: float | None,
    require_gpu_recovery: bool,
) -> dict[str, Any]:
    """Bound and attest release of the Ollama fallback before another CUDA worker."""
    del root
    from scripts.cyntox_council import is_local_ollama_base, ollama_api_base

    started = time.monotonic()
    deadline = started + _RESOURCE_CLEANUP_TIMEOUT_SECONDS
    base_url = ollama_api_base()
    if not is_local_ollama_base(base_url):
        rejected_runner_pids = _ollama_runner_pids()
        remaining = deadline - time.monotonic()
        rejected_observed_vram = (
            _free_vram_mib(timeout=min(1.0, remaining)) if remaining > 0 else None
        )
        finished = time.monotonic()
        elapsed = max(0.001, finished - started)
        bounded = finished <= deadline
        return {
            "performed": True,
            "unload_requested": False,
            "ollama_gpu_clear": False,
            "gpu_recovered": False,
            "bounded": bounded,
            "elapsed_seconds": elapsed,
            "verification": ("rejected_non_loopback" if bounded else "cleanup_deadline_exceeded"),
            "loaded_models": None,
            "runner_pids": rejected_runner_pids,
            "gpu_free_baseline_mib": baseline_vram_mib,
            "gpu_free_after_mib": rejected_observed_vram,
        }

    remaining = deadline - time.monotonic()
    unload_requested = bool(
        remaining > 0 and _request_ollama_unload(base_url, timeout=min(10.0, remaining))
    )
    loaded_models: list[str] | None = None
    runner_pids: list[int] | None = None
    observed_vram: float | None = None
    ollama_gpu_clear = False
    gpu_recovered = not require_gpu_recovery
    verification = "unverified"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        loaded_models = _ollama_loaded_models(base_url, timeout=min(1.0, remaining))
        if time.monotonic() > deadline:
            verification = "cleanup_deadline_exceeded"
            break
        runner_pids = _ollama_runner_pids()
        if time.monotonic() > deadline:
            verification = "cleanup_deadline_exceeded"
            break
        if require_gpu_recovery:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                verification = "cleanup_deadline_exceeded"
                break
            observed_vram = _free_vram_mib(timeout=remaining)
            if time.monotonic() > deadline:
                verification = "cleanup_deadline_exceeded"
                break

        if loaded_models is not None and runner_pids is not None:
            ollama_gpu_clear = not loaded_models and not runner_pids
            verification = "api_and_process"
        elif loaded_models is not None:
            ollama_gpu_clear = False
            verification = "api_only"
        elif runner_pids is not None:
            # Process absence alone cannot prove that Ollama released its model.
            ollama_gpu_clear = False
            verification = "process_only"
        else:
            ollama_gpu_clear = False
            verification = "unverified"
        if require_gpu_recovery:
            gpu_recovered = bool(
                baseline_vram_mib is not None
                and observed_vram is not None
                and observed_vram >= baseline_vram_mib - 256
            )
        if ollama_gpu_clear and gpu_recovered:
            break
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    finished = time.monotonic()
    bounded = finished <= deadline
    if not bounded:
        ollama_gpu_clear = False
        gpu_recovered = False
        verification = "cleanup_deadline_exceeded"
    elapsed = max(0.001, finished - started)
    return {
        "performed": True,
        "unload_requested": unload_requested,
        "ollama_gpu_clear": ollama_gpu_clear,
        "gpu_recovered": gpu_recovered,
        "bounded": bounded,
        "elapsed_seconds": elapsed,
        "verification": verification,
        "loaded_models": loaded_models,
        "runner_pids": runner_pids,
        "gpu_free_baseline_mib": baseline_vram_mib,
        "gpu_free_after_mib": observed_vram,
    }


def _cleanup_result(
    cleanup: ResourceCleanup | None,
    root: Path,
    baseline_vram_mib: float | None,
    require_gpu_recovery: bool,
) -> dict[str, Any]:
    if cleanup is None:
        return {
            "performed": False,
            "unload_requested": False,
            "ollama_gpu_clear": False,
            "gpu_recovered": False,
            "bounded": False,
            "verification": "not_run_for_nonproduction_harness",
        }
    started = time.monotonic()
    try:
        result = dict(cleanup(root, baseline_vram_mib, require_gpu_recovery))
    except BaseException as error:  # noqa: BLE001 - cleanup must become fail-closed evidence
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        elapsed = max(0.001, time.monotonic() - started)
        return {
            "performed": True,
            "unload_requested": False,
            "ollama_gpu_clear": False,
            "gpu_recovered": False,
            "bounded": elapsed <= _RESOURCE_CLEANUP_TIMEOUT_SECONDS,
            "elapsed_seconds": elapsed,
            "verification": (
                "cleanup_error"
                if elapsed <= _RESOURCE_CLEANUP_TIMEOUT_SECONDS
                else "cleanup_deadline_exceeded"
            ),
            "error_type": type(error).__name__,
        }
    elapsed = max(0.001, time.monotonic() - started)
    result.setdefault("performed", True)
    result.setdefault("unload_requested", False)
    result.setdefault("ollama_gpu_clear", False)
    result.setdefault("gpu_recovered", not require_gpu_recovery)
    result["bounded"] = bool(
        result.get("bounded") is True and elapsed <= _RESOURCE_CLEANUP_TIMEOUT_SECONDS
    )
    result["elapsed_seconds"] = elapsed
    if not result["bounded"]:
        result["ollama_gpu_clear"] = False
        result["gpu_recovered"] = False
        result["verification"] = "cleanup_deadline_exceeded"
    return result


def _processes_gone(pids: set[int], *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        gone = all(not psutil.pid_exists(pid) for pid in pids)
        observed_at = time.monotonic()
        if gone:
            return observed_at <= deadline
        if observed_at >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - observed_at)))


def _listener_closed(port: int | None) -> bool:
    if not isinstance(port, int):
        return False
    with socket.socket() as client:
        client.settimeout(0.25)
        return client.connect_ex(("127.0.0.1", port)) != 0


def _only_loopback_connections(pids: set[int]) -> bool:
    try:
        for pid in pids:
            process = psutil.Process(pid)
            for connection in process.net_connections(kind="inet"):
                remote = connection.raddr
                if remote and not ipaddress.ip_address(remote.ip).is_loopback:
                    return False
    except (psutil.Error, ValueError):
        return False
    return True


def _free_vram_mib(*, timeout: float = 10.0) -> float | None:
    if not math.isfinite(timeout) or timeout <= 0:
        return None
    deadline = time.monotonic() + timeout
    executable = shutil.which("nvidia-smi")
    gpu_uuid = selected_gpu_uuid()
    if not executable or not gpu_uuid:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            [
                executable,
                f"--id={gpu_uuid}",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.001, min(10.0, remaining)),
            check=False,
        )
        if time.monotonic() > deadline:
            return None
        value = float(completed.stdout.splitlines()[0].strip())
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _gpu_recovered(before_mib: float | None, *, timeout: float = 30.0) -> bool:
    if before_mib is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = _free_vram_mib(timeout=deadline - time.monotonic())
        observed_at = time.monotonic()
        if observed_at > deadline:
            return False
        if observed is not None and observed >= before_mib - 256:
            return True
        time.sleep(min(0.25, max(0.0, deadline - observed_at)))
    return False


def _identity_matches(
    identity: dict[str, Any],
    *,
    backend: str,
    binding: dict[str, Any],
) -> bool:
    implementation = (
        "transformers.models.qwen3_5.modeling_qwen3_5.Qwen3_5ForCausalLM"
        if backend == "resident"
        else "airllm.airllm_qwen3_5.AirLLMQwen3_5"
    )
    return bool(
        identity.get("backend_kind") == "real"
        and identity.get("backend_name") == _expected_backend(backend)
        and identity.get("implementation_class") == implementation
        and identity.get("model_id") == binding.get("model_id")
        and identity.get("revision") == binding.get("model_revision")
        and identity.get("gpu_uuid") == binding.get("gpu_uuid")
        and identity.get("runtime_lock_sha256") == binding.get("runtime_lock_sha256")
        and identity.get("snapshot_manifest_sha256") == binding.get("snapshot_manifest_sha256")
        and identity.get("shard_manifest_sha256") == binding.get("shard_manifest_sha256")
        and identity.get("qualification_mode") is True
        and isinstance(identity.get("session_id"), str)
        and len(str(identity["session_id"])) == 32
    )


async def _lifecycle_and_egress(
    root: Path,
    backend: str,
    binding: dict[str, Any],
    factory: ProviderFactory,
) -> tuple[dict[str, bool], dict[str, bool], bool, list[str], dict[str, Any]]:
    provider = _new_provider(factory, root, backend)
    before_vram = _free_vram_mib()
    started = False
    port: int | None = None
    pids: set[int] = set()
    identity: dict[str, Any] = {}
    network_blocked = False
    connections_local = False
    try:
        await provider.start(timeout=900)
        started = True
        identity = dict(provider._identity)
        port = provider.port
        pids = _provider_pids(provider)
        network_result = await provider.qualification_fault("network", timeout=5)
        network_blocked = network_result.get("blocked") is True
        connections_local = _only_loopback_connections(pids)
    finally:
        port = provider.port if port is None else port
        pids.update(_provider_pids(provider))
        await provider.close(force=True)
    worker_closed = provider.process is None
    no_orphans = _processes_gone(pids)
    listener_closed = _listener_closed(port)
    live = _identity_matches(identity, backend=backend, binding=binding)
    sessions = [str(identity["session_id"])] if live else []
    lifecycle = {
        "worker_started": started,
        "worker_closed": worker_closed,
        "no_orphan_processes": no_orphans,
        "listener_closed": listener_closed,
        "gpu_state_recovered": _gpu_recovered(before_vram),
    }
    no_egress = {
        "offline_environment": identity.get("offline_environment") is True,
        "socket_guard_blocked": network_blocked,
        "no_external_connections": connections_local,
    }
    diagnostics = {
        "worker_pid": identity.get("pid"),
        "port": port,
        "gpu_free_before_mib": before_vram,
        "backend_kind": identity.get("backend_kind"),
    }
    return lifecycle, no_egress, live, sessions, diagnostics


async def _failure_checks(
    root: Path,
    backend: str,
    binding: dict[str, Any],
    factory: ProviderFactory,
) -> tuple[dict[str, bool], bool, list[str], list[dict[str, Any]]]:
    from scripts.cyntox_council import provider_failure_code

    results: list[dict[str, Any]] = []
    sessions: list[str] = []
    all_real = True
    all_cleanup = True
    for fault, expected in _FAULT_EXPECTATIONS.items():
        provider = _new_provider(factory, root, backend)
        before_vram = _free_vram_mib()
        port: int | None = None
        pids: set[int] = set()
        identity: dict[str, Any] = {}
        observed: str | None = None
        error_type: str | None = None
        try:
            await provider.start(timeout=900)
            identity = dict(provider._identity)
            port = provider.port
            pids = _provider_pids(provider)
            request_timeout = 0.05 if fault == "timeout" else 5.0
            try:
                await provider.qualification_fault(fault, timeout=request_timeout)
            except Exception as error:  # noqa: BLE001 - deliberate fault boundary
                observed = provider_failure_code(error)
                error_type = type(error).__name__
        finally:
            port = provider.port if port is None else port
            pids.update(_provider_pids(provider))
            await provider.close(force=True)
        gpu_recovered = _gpu_recovered(before_vram)
        cleanup = (
            provider.process is None
            and _processes_gone(pids)
            and _listener_closed(port)
            and gpu_recovered
        )
        real = _identity_matches(identity, backend=backend, binding=binding)
        if real:
            sessions.append(str(identity["session_id"]))
        all_real = all_real and real
        all_cleanup = all_cleanup and cleanup
        results.append(
            {
                "fault": fault,
                "expected_classification": expected,
                "observed_classification": observed,
                "error_type": error_type,
                "passed": observed == expected and cleanup,
                "cleanup": cleanup,
                "gpu_state_recovered": gpu_recovered,
                "gpu_free_before_mib": before_vram,
                "gpu_free_after_mib": _free_vram_mib(),
                "backend_kind": identity.get("backend_kind"),
            }
        )
        if not cleanup:
            break
    passed_by_fault = {str(item["fault"]): bool(item["passed"]) for item in results}
    details = {
        "worker_crash_fallback": passed_by_fault.get("crash", False),
        "timeout_fallback": passed_by_fault.get("timeout", False),
        "malformed_output_fallback": passed_by_fault.get("malformed", False),
        "oom_fallback": passed_by_fault.get("oom", False),
        "cleanup_after_each": all_cleanup,
    }
    return details, all_real, sessions, results


def _default_fallback(root: Path) -> bool:
    from scripts.cyntox_council import run_role

    completed = run_role(
        root,
        "A specialist failed during qualification. Reply with exactly: CyntOX fallback ready",
        "15m",
        engine="ollama",
        model=_FALLBACK_MODEL,
        council_mode="plan",
        internet_mode="off",
        allow_domains=[],
    )
    return completed.returncode == 0 and completed.stdout.strip() == "CyntOX fallback ready"


def _pending_details(check: str) -> dict[str, bool]:
    return {field: False for field in airllm_runtime.QUALIFICATION_EVIDENCE_DETAILS[check]}


def run_qualification(
    root: Path,
    *,
    backend: str,
    repetitions: int = airllm_runtime.REQUIRED_QUALIFICATION_REPETITIONS,
    provider_factory: ProviderFactory = AirLlmProvider,
    fallback_runner: FallbackRunner = _default_fallback,
    resource_cleanup: ResourceCleanup | None = None,
) -> dict[str, Any]:
    """Run real-worker qualification while the CLI-owned outer GPU lease remains held."""
    if backend not in {"airllm", "resident"}:
        raise ValueError(f"unsupported qualification backend: {backend}")
    if not 1 <= repetitions <= 10:
        raise ValueError("qualification repetitions must be between 1 and 10")
    backend_name = _expected_backend(backend)
    prepared = airllm_runtime.status(root, verify_hashes=True)
    required = ["runtime_ready", "snapshot_ready", "shards_ready", "offline_reload_proven"]
    if backend == "resident":
        required.append("resident_ready")
    missing = [field for field in required if prepared.get(field) is not True]
    if missing:
        raise RuntimeError("Qwythos runtime is not prepared: " + ", ".join(missing))
    binding = airllm_runtime.current_qualification_binding(root)
    if not airllm_runtime.qualification_binding_is_complete(binding):
        raise RuntimeError("Qwythos qualification binding is incomplete")

    outcomes: list[dict[str, Any]] = []
    trusted_provider_factory = provider_factory is AirLlmProvider
    trusted_fallback_runner = fallback_runner is _default_fallback
    if resource_cleanup is None and trusted_provider_factory and trusted_fallback_runner:
        resource_cleanup = _default_fallback_cleanup
    trusted_resource_cleanup = resource_cleanup is _default_fallback_cleanup
    trusted_harness = (
        trusted_provider_factory and trusted_fallback_runner and trusted_resource_cleanup
    )
    aborted_for_cleanup = False
    for repetition in range(1, repetitions + 1):
        run_id = f"qualification-{backend}-{repetition}-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        attempt_diagnostics: dict[str, Any] = {}
        for check in airllm_runtime.QUALIFICATION_CHECKS:
            airllm_runtime.record_qualification_evidence(
                root,
                check=check,
                backend_name=backend_name,
                passed=False,
                live=trusted_harness,
                hashes_verified=True,
                elapsed_seconds=0.001,
                worker_session_ids=[],
                details=_pending_details(check),
                attempt_status="running",
                run_id=run_id,
            )
        try:
            preflight_cleanup = _cleanup_result(resource_cleanup, root, None, False)
            attempt_diagnostics["fallback_preflight_cleanup"] = preflight_cleanup
            if resource_cleanup is not None and not (
                preflight_cleanup.get("ollama_gpu_clear") is True
                and preflight_cleanup.get("bounded") is True
            ):
                aborted_for_cleanup = True
                raise RuntimeError(
                    "Ollama GPU residency could not be cleared before Qwythos startup"
                )
            lifecycle, no_egress, lifecycle_live, lifecycle_sessions, diagnostics = asyncio.run(
                _lifecycle_and_egress(root, backend, binding, provider_factory)
            )
            diagnostics["fallback_preflight_cleanup"] = preflight_cleanup
            attempt_diagnostics["lifecycle_cleanup"] = lifecycle
            lifecycle_live = lifecycle_live and trusted_harness
            lifecycle_passed = lifecycle_live and all(lifecycle.values())
            no_egress_passed = lifecycle_live and all(no_egress.values())
            elapsed = max(0.001, time.monotonic() - started)
            for check, passed, details in (
                ("lifecycle_cleanup", lifecycle_passed, lifecycle),
                ("no_egress", no_egress_passed, no_egress),
            ):
                airllm_runtime.record_qualification_evidence(
                    root,
                    check=check,
                    backend_name=backend_name,
                    passed=passed,
                    live=lifecycle_live,
                    hashes_verified=lifecycle_live,
                    elapsed_seconds=elapsed,
                    worker_session_ids=lifecycle_sessions,
                    details=details,
                    diagnostics=diagnostics,
                    run_id=run_id,
                )
            if not all(lifecycle.values()):
                aborted_for_cleanup = True
                raise RuntimeError("Qwythos lifecycle cleanup could not be verified")

            failure, failure_live, failure_sessions, fault_results = asyncio.run(
                _failure_checks(root, backend, binding, provider_factory)
            )
            failure_live = failure_live and trusted_harness
            fallback_baseline_vram = _free_vram_mib()
            fallback_error: BaseException | None = None
            qwythos_cleanup_safe = failure.get("cleanup_after_each") is True
            fallback_executed = qwythos_cleanup_safe and not (
                not trusted_provider_factory and trusted_fallback_runner
            )
            fallback_skip_reason: str | None = None
            if not qwythos_cleanup_safe:
                fallback_skip_reason = "qwythos_cleanup_unverified"
                aborted_for_cleanup = True
            elif not fallback_executed:
                fallback_skip_reason = "nonproduction_default_fallback"
            if fallback_executed:
                try:
                    fallback_passed = bool(fallback_runner(root))
                except BaseException as error:  # noqa: BLE001 - cleanup must still run
                    fallback_error = error
                    fallback_passed = False
                finally:
                    post_fallback_cleanup = _cleanup_result(
                        resource_cleanup,
                        root,
                        fallback_baseline_vram,
                        True,
                    )
            else:
                fallback_passed = False
                post_fallback_cleanup = _cleanup_result(None, root, fallback_baseline_vram, True)
            if isinstance(fallback_error, KeyboardInterrupt | SystemExit):
                raise fallback_error
            failure = {
                key: value and fallback_passed if key.endswith("_fallback") else value
                for key, value in failure.items()
            }
            failure.update(
                {
                    "fallback_model_absent_before_worker": (
                        preflight_cleanup.get("ollama_gpu_clear") is True
                    ),
                    "fallback_unload_requested": (
                        post_fallback_cleanup.get("unload_requested") is True
                    ),
                    "fallback_model_released": (
                        post_fallback_cleanup.get("ollama_gpu_clear") is True
                    ),
                    "fallback_gpu_recovered": (post_fallback_cleanup.get("gpu_recovered") is True),
                    "fallback_cleanup_bounded": (post_fallback_cleanup.get("bounded") is True),
                }
            )
            failure_passed = failure_live and fallback_passed and all(failure.values())
            elapsed = max(0.001, time.monotonic() - started)
            airllm_runtime.record_qualification_evidence(
                root,
                check="failure_fallback",
                backend_name=backend_name,
                passed=failure_passed,
                live=failure_live,
                hashes_verified=failure_live,
                elapsed_seconds=elapsed,
                worker_session_ids=failure_sessions,
                details=failure,
                diagnostics={
                    "fault_results": fault_results,
                    "fallback_provider": "cyntox",
                    "fallback_executed": fallback_executed,
                    "fallback_skip_reason": fallback_skip_reason,
                    "fallback_passed": fallback_passed,
                    "fallback_error_type": (
                        type(fallback_error).__name__ if fallback_error is not None else None
                    ),
                    "fallback_preflight_cleanup": preflight_cleanup,
                    "fallback_post_cleanup": post_fallback_cleanup,
                },
                run_id=run_id,
            )
            outcomes.append(
                {
                    "run_id": run_id,
                    "repetition": repetition,
                    "live": lifecycle_live and failure_live,
                    "lifecycle_cleanup": lifecycle,
                    "no_egress": no_egress,
                    "failure_fallback": failure,
                    "fault_results": fault_results,
                    "fallback_executed": fallback_executed,
                    "fallback_skip_reason": fallback_skip_reason,
                    "fallback_passed": fallback_passed,
                    "fallback_error_type": (
                        type(fallback_error).__name__ if fallback_error is not None else None
                    ),
                    "fallback_preflight_cleanup": preflight_cleanup,
                    "fallback_post_cleanup": post_fallback_cleanup,
                    "diagnostics": diagnostics,
                }
            )
            if resource_cleanup is not None and not all(
                (
                    failure["fallback_model_released"],
                    failure["fallback_gpu_recovered"],
                    failure["fallback_cleanup_bounded"],
                )
            ):
                aborted_for_cleanup = True
        except BaseException as error:  # noqa: BLE001 - persist terminal harness failures
            elapsed = max(0.001, time.monotonic() - started)
            for check in airllm_runtime.QUALIFICATION_CHECKS:
                airllm_runtime.record_qualification_evidence(
                    root,
                    check=check,
                    backend_name=backend_name,
                    passed=False,
                    live=trusted_harness,
                    hashes_verified=False,
                    elapsed_seconds=elapsed,
                    worker_session_ids=[],
                    details=_pending_details(check),
                    diagnostics={
                        **attempt_diagnostics,
                        "failure_type": type(error).__name__,
                    },
                    run_id=run_id,
                )
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            outcomes.append(
                {
                    "run_id": run_id,
                    "repetition": repetition,
                    "live": False,
                    "failure_type": type(error).__name__,
                    "diagnostics": attempt_diagnostics,
                }
            )
        if aborted_for_cleanup:
            break

    evidence = airllm_runtime.qualification_evidence_status(
        root,
        backend_name=backend_name,
        binding=binding,
    )
    return {
        "backend": backend,
        "backend_name": backend_name,
        "repetitions": repetitions,
        "completed_repetitions": len(outcomes),
        "aborted_for_cleanup": aborted_for_cleanup,
        "qualification_binding": binding,
        "qualification_binding_sha256": airllm_runtime.qualification_binding_sha256(binding),
        "outcomes": outcomes,
        "evidence": evidence,
        "passed": evidence["valid"],
    }
