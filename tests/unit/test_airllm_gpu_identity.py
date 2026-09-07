# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oslab import airllm_runtime
from oslab.model import airllm as airllm_provider_module
from oslab.model.airllm import AirLlmProvider
from scripts import cyntox_airllm_worker

ROOT = Path(__file__).resolve().parents[2]
GPU_UUID = "GPU-8f460594-ebbc-8887-4647-d40c02f41157"


class _FakeCuda:
    def __init__(self, *, uuid: str = GPU_UUID.removeprefix("GPU-"), peak_mib: float = 0) -> None:
        self.uuid = uuid
        self.peak_bytes = peak_mib * 1024 * 1024

    @staticmethod
    def is_available() -> bool:
        return True

    def get_device_properties(self, index: int) -> SimpleNamespace:
        assert index == 0
        return SimpleNamespace(uuid=self.uuid)

    def max_memory_reserved(self, index: int) -> float:
        assert index == 0
        return self.peak_bytes


def _fake_torch(*, uuid: str = GPU_UUID.removeprefix("GPU-"), peak_mib: float = 0) -> Any:
    return SimpleNamespace(cuda=_FakeCuda(uuid=uuid, peak_mib=peak_mib))


def _binding() -> dict[str, object]:
    digest = "a" * 64
    return {
        "schema_version": airllm_runtime.QUALIFICATION_BINDING_SCHEMA_VERSION,
        "model_id": "locked/model",
        "model_revision": "b" * 40,
        "precision": "bf16",
        "model_context_limit": 32_768,
        "implementation_sha256": {
            name: digest for name, _path in airllm_runtime.QUALIFICATION_IMPLEMENTATION_PATHS
        },
        "model_lock_sha256": digest,
        "runtime_lock_sha256": digest,
        "snapshot_manifest_sha256": digest,
        "shard_manifest_sha256": digest,
        "gpu_uuid": GPU_UUID,
    }


def _qualification(binding: dict[str, object]) -> dict[str, object]:
    smoke_spec = airllm_runtime.qualification_smoke_spec("airllm")
    return {
        "schema_version": airllm_runtime.QUALIFICATION_RECORD_SCHEMA_VERSION,
        "attempt_status": "passed",
        "passed": True,
        "hashes_verified": True,
        "model_id": binding["model_id"],
        "model_revision": binding["model_revision"],
        "architecture": "Qwen3_5ForConditionalGeneration",
        "backend_kind": "real",
        "backend": smoke_spec.backend,
        "backend_name": "airllm",
        "implementation_class": "airllm.airllm_qwen3_5.AirLLMQwen3_5",
        "context_limit": 32_768,
        "qualification_binding": binding,
        "qualification_binding_sha256": airllm_runtime.qualification_binding_sha256(binding),
        "model_lock_sha256": binding["model_lock_sha256"],
        "runtime_lock_sha256": binding["runtime_lock_sha256"],
        "snapshot_manifest_sha256": binding["snapshot_manifest_sha256"],
        "shard_manifest_sha256": binding["shard_manifest_sha256"],
        "gpu_uuid": binding["gpu_uuid"],
        "worker_pid": 1234,
        "worker_session_id": "c" * 32,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "max_new_tokens": smoke_spec.max_new_tokens,
        "prompt_hash": smoke_spec.prompt_hash,
        "response_hash": smoke_spec.response_hash,
        "seed": smoke_spec.seed,
        "elapsed_seconds": 5.0,
        "startup_peak_vram_mib": 2_000.0,
        "peak_vram_mib": 2_100.0,
        "started_at": "2026-09-05T12:00:00+00:00",
        "ended_at": "2026-09-05T12:00:05+00:00",
        "created_at": "2026-09-05T12:00:06+00:00",
    }


def test_worker_verifies_parent_gpu_against_cuda_device_zero() -> None:
    assert cyntox_airllm_worker._verified_gpu_uuid(_fake_torch(), GPU_UUID) == GPU_UUID
    with pytest.raises(RuntimeError, match="does not match"):
        cyntox_airllm_worker._verified_gpu_uuid(_fake_torch(uuid="different"), GPU_UUID)
    with pytest.raises(RuntimeError, match="no selected"):
        cyntox_airllm_worker._verified_gpu_uuid(_fake_torch(), None)


def test_peak_measurement_never_discards_backend_startup_peak() -> None:
    assert cyntox_airllm_worker._startup_inclusive_peak_mib(
        _fake_torch(peak_mib=1_000), 2_000
    ) == pytest.approx(2_000)
    assert cyntox_airllm_worker._startup_inclusive_peak_mib(
        _fake_torch(peak_mib=3_000), 2_000
    ) == pytest.approx(3_000)


def test_worker_environment_passes_only_resolved_gpu_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("CYNTOX_GPU_UUID", "untrusted-parent-value")
    monkeypatch.setattr(airllm_runtime, "selected_gpu_uuid", lambda: GPU_UUID)

    environment = airllm_runtime.worker_environment(ROOT)

    assert environment["CYNTOX_GPU_UUID"] == GPU_UUID


def test_fake_protocol_carries_gpu_and_startup_peak_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(tmp_path / "runtime"))
    monkeypatch.setattr(airllm_runtime, "selected_gpu_uuid", lambda: GPU_UUID)
    monkeypatch.setattr(airllm_provider_module, "selected_gpu_uuid", lambda: GPU_UUID)
    provider = AirLlmProvider(
        ROOT,
        worker_python=Path(sys.executable),
        fake=True,
        manage_gpu_lease=False,
        max_new_tokens=32,
    )

    async def exercise() -> None:
        try:
            response = await provider.complete(
                [{"role": "user", "content": "reply briefly"}], timeout=10
            )
            assert response.content
            assert provider._identity["gpu_uuid"] == GPU_UUID
            assert provider._identity["startup_peak_vram_mib"] == 0.0
            assert provider.last_metadata["gpu_uuid"] == GPU_UUID
            assert provider.last_metadata["startup_peak_vram_mib"] == 0.0
            assert provider.last_metadata["peak_vram_mib"] >= 0.0
        finally:
            await provider.close()

    asyncio.run(exercise())


def test_qualification_is_bound_to_gpu_and_startup_inclusive_peak() -> None:
    binding = _binding()
    qualification = _qualification(binding)
    assert airllm_runtime.valid_qualification_record(
        qualification, binding=binding, backend_name="airllm"
    )

    wrong_gpu = {**qualification, "gpu_uuid": "GPU-different"}
    assert not airllm_runtime.valid_qualification_record(
        wrong_gpu, binding=binding, backend_name="airllm"
    )

    understated = {**qualification, "peak_vram_mib": 1_999.0}
    assert not airllm_runtime.valid_qualification_record(
        understated, binding=binding, backend_name="airllm"
    )

    non_finite = {**qualification, "startup_peak_vram_mib": math.inf}
    assert not airllm_runtime.valid_qualification_record(
        non_finite, binding=binding, backend_name="airllm"
    )


def test_worker_rejects_snapshot_to_shard_manifest_cross_bind_tampering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot_manifest = tmp_path / "snapshot-manifest.json"
    shard_manifest = tmp_path / "shard-manifest.json"
    shard_manifest.write_text('{"tampered":true}', encoding="utf-8")
    snapshot_manifest.write_text(
        json.dumps(
            {
                "model_id": cyntox_airllm_worker.MODEL_ID,
                "revision": cyntox_airllm_worker.REVISION,
                "actual_snapshot_bytes": cyntox_airllm_worker.EXPECTED_BYTES,
                "architecture": "Qwen3_5ForConditionalGeneration",
                "trust_remote_code": False,
                "offline_reload_proven": True,
                "shard_manifest_sha256": "0" * 64,
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cyntox_airllm_worker, "_snapshot_manifest_path", lambda: snapshot_manifest)
    monkeypatch.setattr(cyntox_airllm_worker, "_shard_manifest_path", lambda: shard_manifest)

    with pytest.raises(RuntimeError, match="snapshot manifest does not match"):
        cyntox_airllm_worker._validate_runtime(deep=False)


@pytest.mark.parametrize(
    "readiness_field",
    ["runtime_ready", "snapshot_ready", "shards_ready", "offline_reload_proven"],
)
def test_provider_rejects_worker_when_runtime_status_is_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, readiness_field: str
) -> None:
    provider = AirLlmProvider(ROOT, fake=False, manage_gpu_lease=False)
    provider.process = SimpleNamespace(pid=12_345)  # type: ignore[assignment]
    provider._expected_gpu_uuid = GPU_UUID
    prepared: dict[str, object] = {
        "home": str(tmp_path),
        "runtime_ready": True,
        "snapshot_ready": True,
        "shards_ready": True,
        "offline_reload_proven": True,
        "resident_ready": True,
        "native_optional_kernels": False,
        "manifest": {"runtime_lock_sha256": "runtime-lock"},
    }
    prepared[readiness_field] = False
    monkeypatch.setattr(airllm_provider_module, "status", lambda _root: prepared)
    monkeypatch.setattr(
        airllm_provider_module,
        "manifest_hash",
        lambda path: "snapshot-hash" if path.name.startswith("snapshot") else "shard-hash",
    )
    monkeypatch.setattr(
        airllm_provider_module.psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: []),
    )
    identity = {
        "protocol": 1,
        "pid": 12_345,
        "model_id": "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated",
        "revision": "efcc73cac15ff8fc5d46b8d41b53c22d571cf97d",
        "architecture": "Qwen3_5ForConditionalGeneration",
        "precision": "bf16",
        "context_limit": 32_768,
        "gpu_uuid": GPU_UUID,
        "startup_peak_vram_mib": 2_000.0,
        "backend_kind": "real",
        "backend_name": "airllm",
        "implementation_class": "airllm.airllm_qwen3_5.AirLLMQwen3_5",
        "qualification_mode": False,
        "session_id": "a" * 32,
        "port": 49_152,
        "token": "b" * 32,
        "runtime_lock_sha256": "runtime-lock",
        "snapshot_manifest_sha256": "snapshot-hash",
        "shard_manifest_sha256": "shard-hash",
    }

    with pytest.raises(RuntimeError, match=readiness_field):
        provider._validate_identity(identity, handshake=True)
