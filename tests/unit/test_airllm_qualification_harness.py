# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from oslab import airllm_qualification, airllm_runtime


def _binding() -> dict[str, Any]:
    return {
        "schema_version": airllm_runtime.QUALIFICATION_BINDING_SCHEMA_VERSION,
        "model_id": "locked/model",
        "model_revision": "e" * 40,
        "precision": "bf16",
        "model_context_limit": 32_768,
        "implementation_sha256": {
            name: "f" * 64 for name, _path in airllm_runtime.QUALIFICATION_IMPLEMENTATION_PATHS
        },
        "model_lock_sha256": "a" * 64,
        "runtime_lock_sha256": "b" * 64,
        "snapshot_manifest_sha256": "c" * 64,
        "shard_manifest_sha256": "d" * 64,
        "gpu_uuid": "GPU-test",
    }


class FakeQualificationProvider:
    counter = 0

    def __init__(self, *_args: object, backend: str, **_kwargs: object) -> None:
        type(self).counter += 1
        session = f"{type(self).counter:032x}"
        self.backend = backend
        self.process = None
        self.worker_pid = None
        self.port = 31_337
        self._identity = {
            "pid": 42,
            "session_id": session,
            "backend_kind": "fake",
            "backend_name": "fake",
            "implementation_class": "fake",
            "model_id": "locked/model",
            "revision": "e" * 40,
            "gpu_uuid": "GPU-test",
            "runtime_lock_sha256": "b" * 64,
            "snapshot_manifest_sha256": "c" * 64,
            "shard_manifest_sha256": "d" * 64,
            "qualification_mode": True,
            "offline_environment": True,
        }

    async def start(self, *, timeout: float) -> None:
        assert timeout == 900

    async def qualification_fault(self, fault: str, *, timeout: float) -> dict[str, Any]:
        if fault == "network":
            return {"session_id": self._identity["session_id"], "blocked": True}
        if fault == "timeout":
            raise TimeoutError("qualification timeout")
        if fault == "malformed":
            raise ValueError("malformed output")
        if fault == "oom":
            raise RuntimeError("CUDA out of memory")
        raise RuntimeError("worker crashed")

    async def close(self, *, force: bool = False) -> None:
        assert force is True
        self.process = None


def _prepare_fake_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    home = tmp_path / "runtime"
    binding = _binding()
    monkeypatch.setattr(airllm_runtime, "runtime_home", lambda: home)
    monkeypatch.setattr(
        airllm_runtime,
        "status",
        lambda *_args, **_kwargs: {
            "runtime_ready": True,
            "snapshot_ready": True,
            "shards_ready": True,
            "offline_reload_proven": True,
            "hashes_verified": True,
        },
    )
    monkeypatch.setattr(
        airllm_runtime,
        "current_qualification_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(airllm_qualification, "_free_vram_mib", lambda **_kwargs: 8_192.0)
    monkeypatch.setattr(airllm_qualification, "_gpu_recovered", lambda _before: True)
    monkeypatch.setattr(airllm_qualification, "_only_loopback_connections", lambda _pids: True)
    monkeypatch.setattr(airllm_qualification, "_listener_closed", lambda _port: True)
    monkeypatch.setattr(airllm_qualification, "_processes_gone", lambda _pids: True)
    return home


def test_fake_harness_exercises_protocol_but_cannot_emit_live_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _prepare_fake_runtime(tmp_path, monkeypatch)

    report = airllm_qualification.run_qualification(
        tmp_path,
        backend="airllm",
        repetitions=1,
        provider_factory=FakeQualificationProvider,  # type: ignore[arg-type]
        fallback_runner=lambda _root: True,
    )

    assert report["passed"] is False
    assert report["outcomes"][0]["live"] is False
    assert all(item["passed"] for item in report["outcomes"][0]["fault_results"])
    assert report["evidence"]["valid_record_count"] == 0
    assert report["evidence"]["valid"] is False
    persisted = airllm_runtime.qualification_evidence_path(home)
    assert persisted.is_file()
    records = json.loads(persisted.read_text(encoding="utf-8"))["records"]
    assert records and all(record["live"] is False for record in records)


def test_fake_provider_never_invokes_default_real_ollama_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_fake_runtime(tmp_path, monkeypatch)
    FakeQualificationProvider.counter = 0

    report = airllm_qualification.run_qualification(
        tmp_path,
        backend="airllm",
        repetitions=1,
        provider_factory=FakeQualificationProvider,  # type: ignore[arg-type]
    )

    assert report["outcomes"][0]["fallback_executed"] is False
    assert report["outcomes"][0]["fallback_passed"] is False
    assert report["outcomes"][0]["fallback_preflight_cleanup"]["performed"] is False
    assert report["outcomes"][0]["fallback_post_cleanup"]["performed"] is False


def test_cleanup_boundary_runs_before_next_repetition_without_real_ollama(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_fake_runtime(tmp_path, monkeypatch)
    FakeQualificationProvider.counter = 0
    cleanup_calls: list[tuple[int, bool]] = []
    fallback_provider_counts: list[int] = []

    def cleanup(_root: Path, _baseline: float | None, require_gpu: bool) -> dict[str, Any]:
        cleanup_calls.append((FakeQualificationProvider.counter, require_gpu))
        return {
            "performed": True,
            "unload_requested": True,
            "ollama_gpu_clear": True,
            "gpu_recovered": True,
            "bounded": True,
        }

    def fallback(_root: Path) -> bool:
        fallback_provider_counts.append(FakeQualificationProvider.counter)
        return True

    report = airllm_qualification.run_qualification(
        tmp_path,
        backend="airllm",
        repetitions=2,
        provider_factory=FakeQualificationProvider,  # type: ignore[arg-type]
        fallback_runner=fallback,
        resource_cleanup=cleanup,
    )

    assert report["completed_repetitions"] == 2
    assert report["aborted_for_cleanup"] is False
    assert cleanup_calls == [(0, False), (5, True), (5, False), (10, True)]
    assert fallback_provider_counts == [5, 10]
    assert all(
        outcome["failure_fallback"]["fallback_model_released"]
        and outcome["failure_fallback"]["fallback_gpu_recovered"]
        for outcome in report["outcomes"]
    )


def test_failed_post_fallback_cleanup_prevents_next_worker_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_fake_runtime(tmp_path, monkeypatch)
    FakeQualificationProvider.counter = 0

    def cleanup(_root: Path, _baseline: float | None, require_gpu: bool) -> dict[str, Any]:
        return {
            "performed": True,
            "unload_requested": True,
            "ollama_gpu_clear": not require_gpu,
            "gpu_recovered": not require_gpu,
            "bounded": True,
        }

    report = airllm_qualification.run_qualification(
        tmp_path,
        backend="airllm",
        repetitions=3,
        provider_factory=FakeQualificationProvider,  # type: ignore[arg-type]
        fallback_runner=lambda _root: True,
        resource_cleanup=cleanup,
    )

    assert report["completed_repetitions"] == 1
    assert report["aborted_for_cleanup"] is True
    assert FakeQualificationProvider.counter == 5
    assert report["outcomes"][0]["failure_fallback"]["fallback_model_released"] is False


def test_failed_preflight_cleanup_prevents_any_worker_start_and_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_fake_runtime(tmp_path, monkeypatch)
    FakeQualificationProvider.counter = 0

    def cleanup(_root: Path, _baseline: float | None, _require_gpu: bool) -> dict[str, Any]:
        return {
            "performed": True,
            "unload_requested": False,
            "ollama_gpu_clear": False,
            "gpu_recovered": False,
            "bounded": True,
            "verification": "resident_runner_remained",
        }

    report = airllm_qualification.run_qualification(
        tmp_path,
        backend="airllm",
        repetitions=3,
        provider_factory=FakeQualificationProvider,  # type: ignore[arg-type]
        fallback_runner=lambda _root: True,
        resource_cleanup=cleanup,
    )

    assert report["completed_repetitions"] == 1
    assert report["aborted_for_cleanup"] is True
    assert FakeQualificationProvider.counter == 0
    outcome = report["outcomes"][0]
    assert outcome["failure_type"] == "RuntimeError"
    assert outcome["diagnostics"]["fallback_preflight_cleanup"]["verification"] == (
        "resident_runner_remained"
    )


def test_failed_lifecycle_gpu_recovery_prevents_fault_workers_and_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_fake_runtime(tmp_path, monkeypatch)
    FakeQualificationProvider.counter = 0
    fallback_calls = 0

    def cleanup(_root: Path, _baseline: float | None, _require_gpu: bool) -> dict[str, Any]:
        return {
            "unload_requested": True,
            "ollama_gpu_clear": True,
            "gpu_recovered": True,
            "bounded": True,
        }

    def fallback(_root: Path) -> bool:
        nonlocal fallback_calls
        fallback_calls += 1
        return True

    monkeypatch.setattr(airllm_qualification, "_gpu_recovered", lambda _before: False)
    report = airllm_qualification.run_qualification(
        tmp_path,
        backend="airllm",
        repetitions=3,
        provider_factory=FakeQualificationProvider,  # type: ignore[arg-type]
        fallback_runner=fallback,
        resource_cleanup=cleanup,
    )

    assert report["aborted_for_cleanup"] is True
    assert FakeQualificationProvider.counter == 1
    assert fallback_calls == 0
    assert report["outcomes"][0]["diagnostics"]["lifecycle_cleanup"]["gpu_state_recovered"] is False


def test_failed_fault_worker_gpu_recovery_skips_cyntox_and_remaining_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_fake_runtime(tmp_path, monkeypatch)
    FakeQualificationProvider.counter = 0
    recoveries = iter((True, False))
    fallback_calls = 0

    def cleanup(_root: Path, _baseline: float | None, _require_gpu: bool) -> dict[str, Any]:
        return {
            "unload_requested": True,
            "ollama_gpu_clear": True,
            "gpu_recovered": True,
            "bounded": True,
        }

    def fallback(_root: Path) -> bool:
        nonlocal fallback_calls
        fallback_calls += 1
        return True

    monkeypatch.setattr(
        airllm_qualification,
        "_gpu_recovered",
        lambda _before: next(recoveries),
    )
    report = airllm_qualification.run_qualification(
        tmp_path,
        backend="airllm",
        repetitions=3,
        provider_factory=FakeQualificationProvider,  # type: ignore[arg-type]
        fallback_runner=fallback,
        resource_cleanup=cleanup,
    )

    outcome = report["outcomes"][0]
    assert report["aborted_for_cleanup"] is True
    assert FakeQualificationProvider.counter == 2
    assert fallback_calls == 0
    assert len(outcome["fault_results"]) == 1
    assert outcome["fault_results"][0]["gpu_state_recovered"] is False
    assert outcome["fallback_executed"] is False
    assert outcome["fallback_skip_reason"] == "qwythos_cleanup_unverified"


def test_default_cleanup_attests_loopback_inventory_processes_and_vram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import cyntox_council

    monkeypatch.setattr(cyntox_council, "ollama_api_base", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(airllm_qualification, "_request_ollama_unload", lambda *_a, **_k: True)
    monkeypatch.setattr(airllm_qualification, "_ollama_loaded_models", lambda *_a, **_k: [])
    monkeypatch.setattr(airllm_qualification, "_ollama_runner_pids", lambda: [])
    monkeypatch.setattr(airllm_qualification, "_free_vram_mib", lambda **_kwargs: 8_000.0)

    result = airllm_qualification._default_fallback_cleanup(tmp_path, 8_192.0, True)

    assert result["unload_requested"] is True
    assert result["ollama_gpu_clear"] is True
    assert result["gpu_recovered"] is True
    assert result["verification"] == "api_and_process"
    assert result["loaded_models"] == []
    assert result["runner_pids"] == []


def test_default_cleanup_rejects_attestation_completed_after_absolute_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import cyntox_council

    clock = {"now": 0.0}
    runner_probed = False

    def late_inventory(*_args: object, **_kwargs: object) -> list[str]:
        clock["now"] = 31.0
        return []

    def runner_inventory() -> list[int]:
        nonlocal runner_probed
        runner_probed = True
        return []

    monkeypatch.setattr(cyntox_council, "ollama_api_base", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(airllm_qualification.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(airllm_qualification, "_request_ollama_unload", lambda *_a, **_k: True)
    monkeypatch.setattr(airllm_qualification, "_ollama_loaded_models", late_inventory)
    monkeypatch.setattr(airllm_qualification, "_ollama_runner_pids", runner_inventory)

    result = airllm_qualification._default_fallback_cleanup(tmp_path, None, False)

    assert result["bounded"] is False
    assert result["ollama_gpu_clear"] is False
    assert result["gpu_recovered"] is False
    assert result["verification"] == "cleanup_deadline_exceeded"
    assert runner_probed is False


def test_default_cleanup_never_uses_process_only_evidence_as_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import cyntox_council

    clock = {"now": 0.0}

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(cyntox_council, "ollama_api_base", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(airllm_qualification, "_RESOURCE_CLEANUP_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(airllm_qualification.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(airllm_qualification.time, "sleep", advance)
    monkeypatch.setattr(airllm_qualification, "_request_ollama_unload", lambda *_a, **_k: True)
    monkeypatch.setattr(airllm_qualification, "_ollama_loaded_models", lambda *_a, **_k: None)
    monkeypatch.setattr(airllm_qualification, "_ollama_runner_pids", lambda: [])

    result = airllm_qualification._default_fallback_cleanup(tmp_path, None, False)

    assert result["bounded"] is True
    assert result["ollama_gpu_clear"] is False
    assert result["verification"] == "process_only"
    assert result["runner_pids"] == []


def test_cleanup_wrapper_overrides_late_self_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}

    def cleanup(_root: Path, _baseline: float | None, _require_gpu: bool) -> dict[str, Any]:
        clock["now"] = 31.0
        return {
            "ollama_gpu_clear": True,
            "gpu_recovered": True,
            "bounded": True,
            "elapsed_seconds": 0.001,
            "verification": "api_and_process",
        }

    monkeypatch.setattr(airllm_qualification.time, "monotonic", lambda: clock["now"])

    result = airllm_qualification._cleanup_result(cleanup, tmp_path, 8_192.0, True)

    assert result["bounded"] is False
    assert result["ollama_gpu_clear"] is False
    assert result["gpu_recovered"] is False
    assert result["elapsed_seconds"] == 31.0
    assert result["verification"] == "cleanup_deadline_exceeded"


def test_gpu_recovery_rejects_a_late_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}

    def late_vram_probe(**_kwargs: object) -> float:
        clock["now"] = 31.0
        return 8_192.0

    monkeypatch.setattr(airllm_qualification.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(airllm_qualification, "_free_vram_mib", late_vram_probe)

    assert airllm_qualification._gpu_recovered(8_192.0, timeout=30.0) is False


def test_free_vram_probe_rejects_result_returned_after_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}

    def late_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        clock["now"] = 2.0
        return subprocess.CompletedProcess(["nvidia-smi"], 0, "8192\n", "")

    monkeypatch.setattr(airllm_qualification.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(airllm_qualification.shutil, "which", lambda _name: "nvidia-smi")
    monkeypatch.setattr(airllm_qualification, "selected_gpu_uuid", lambda: "GPU-test")
    monkeypatch.setattr(airllm_qualification.subprocess, "run", late_run)

    assert airllm_qualification._free_vram_mib(timeout=1.0) is None


def test_runner_inventory_fails_closed_when_process_identity_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreadableProcess:
        def cmdline(self) -> list[str]:
            raise airllm_qualification.psutil.AccessDenied(pid=123)

    monkeypatch.setattr(airllm_qualification.os, "name", "nt")
    monkeypatch.setattr(
        airllm_qualification.psutil,
        "Process",
        lambda _pid: UnreadableProcess(),
    )
    monkeypatch.setattr(
        airllm_qualification,
        "_windows_process_names",
        lambda: [(123, "ollama.exe")],
    )

    assert airllm_qualification._ollama_runner_pids() is None


def test_default_fallback_requires_exact_cyntox_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import cyntox_council

    monkeypatch.setattr(
        cyntox_council,
        "run_role",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ollama-api"], 0, "CyntOX fallback ready-ish", ""
        ),
    )
    assert airllm_qualification._default_fallback(tmp_path) is False

    monkeypatch.setattr(
        cyntox_council,
        "run_role",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ollama-api"], 0, " CyntOX fallback ready\n", ""
        ),
    )
    assert airllm_qualification._default_fallback(tmp_path) is True


def test_qualification_repetition_count_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 10"):
        airllm_qualification.run_qualification(tmp_path, backend="airllm", repetitions=0)
