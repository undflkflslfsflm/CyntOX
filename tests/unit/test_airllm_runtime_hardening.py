# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from oslab import airllm_qualification, airllm_runtime
from oslab.model_registry import locked_model
from oslab.resource_lease import active_resource_kind
from scripts import cyntox_cli

MODEL_ID = "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated"
REVISION = "efcc73cac15ff8fc5d46b8d41b53c22d571cf97d"
EXPECTED_SNAPSHOT_BYTES = 19_333_096_957
GPU_UUID = "GPU-8f460594-ebbc-8887-4647-d40c02f41157"


@pytest.fixture(autouse=True)
def selected_physical_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(airllm_runtime, "selected_gpu_uuid", lambda: GPU_UUID)
    real_locked_model = airllm_runtime.locked_model

    def fixture_locked_model(root: Path, name: str) -> object:
        return replace(real_locked_model(root, name), expected_snapshot_bytes=len(b"snapshot"))

    # Runtime-integrity fixtures use an eight-byte checkpoint so deep status tests stay fast.
    # Registry-policy tests below call model_registry.locked_model directly and still exercise
    # the production 19,333,096,957-byte lock.
    monkeypatch.setattr(airllm_runtime, "locked_model", fixture_locked_model)
    monkeypatch.setattr(
        airllm_runtime,
        "locked_qwythos_snapshot_files",
        lambda _root: (
            {
                "path": "model.safetensors",
                "size": len(b"snapshot"),
                "sha256": hashlib.sha256(b"snapshot").hexdigest(),
            },
        ),
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _locked_fields() -> dict[str, object]:
    return {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "precision": "bf16",
        "expected_snapshot_bytes": EXPECTED_SNAPSHOT_BYTES,
        "context_limit": 32_768,
        "trust_remote_code": False,
    }


def _write_project_locks(root: Path) -> Path:
    fields = _locked_fields()
    models = root / "config" / "models.toml"
    models.parent.mkdir(parents=True, exist_ok=True)
    models.write_text(
        "\n".join(
            (
                "[models.qwythos-airllm]",
                f'model_id = "{fields["model_id"]}"',
                f'revision = "{fields["revision"]}"',
                f'precision = "{fields["precision"]}"',
                f"expected_snapshot_bytes = {fields['expected_snapshot_bytes']}",
                f"context_limit = {fields['context_limit']}",
                "trust_remote_code = false",
                'provider = "airllm"',
                "",
            )
        ),
        encoding="utf-8",
    )
    runtime = root / "runtimes" / "airllm"
    _write_json(
        runtime / "model.lock.json",
        {
            **fields,
            "snapshot_files": json.loads(
                (
                    Path(__file__).resolve().parents[2] / "runtimes" / "airllm" / "model.lock.json"
                ).read_text(encoding="utf-8")
            )["snapshot_files"],
            "sampling": {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "repetition_penalty": 1.05,
                "max_new_tokens": 2_048,
            },
        },
    )
    requirements = runtime / "requirements.lock.txt"
    requirements.write_text(
        "airllm==3.3.0 \\\n+    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )
    for name, relative_path in airllm_runtime.QUALIFICATION_IMPLEMENTATION_PATHS:
        implementation_path = root / relative_path
        if implementation_path.is_file():
            continue
        implementation_path.parent.mkdir(parents=True, exist_ok=True)
        implementation_path.write_text(f"# qualification fixture: {name}\n", encoding="utf-8")
    return requirements


def _write_valid_runtime(root: Path, home: Path) -> dict[str, Path]:
    requirements = _write_project_locks(root)
    python = airllm_runtime.runtime_python(home)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()

    snapshot = home / "model" / "model.safetensors"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"snapshot")
    snapshot_hash = airllm_runtime.manifest_hash(snapshot)

    shard = home / "layer-shards" / "splitted_model" / "layer-0.safetensors"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_bytes(b"shard")
    shard_manifest = home / "shard-manifest.json"
    _write_json(
        shard_manifest,
        {
            "model_id": MODEL_ID,
            "revision": REVISION,
            "source_sha256": snapshot_hash,
            "source_tensor_count": 2,
            "sharded_tensor_count": 1,
            "excluded_tensor_count": 1,
            "excluded_tensors": ["mtp.proj.weight"],
            "files": [
                {
                    "path": shard.name,
                    "size": shard.stat().st_size,
                    "sha256": airllm_runtime.manifest_hash(shard),
                    "tensor_count": 1,
                    "tensors": ["model.language_model.layers.0.weight"],
                }
            ],
        },
    )
    manifest = home / "snapshot-manifest.json"
    _write_json(
        manifest,
        {
            **_locked_fields(),
            "actual_snapshot_bytes": snapshot.stat().st_size,
            "architecture": "Qwen3_5ForConditionalGeneration",
            "runtime_lock_sha256": airllm_runtime.manifest_hash(requirements),
            "shard_manifest_sha256": airllm_runtime.manifest_hash(shard_manifest),
            "offline_reload_proven": True,
            "files": [
                {
                    "path": snapshot.name,
                    "size": snapshot.stat().st_size,
                    "sha256": snapshot_hash,
                }
            ],
        },
    )
    _write_json(
        home / "resident-model" / "model.safetensors.index.json",
        {
            "weight_map": {
                "model.language_model.layers.0.weight": (
                    f"../layer-shards/splitted_model/{shard.name}"
                )
            }
        },
    )
    shard.with_suffix(shard.suffix + ".done").touch()
    return {
        "manifest": manifest,
        "requirements": requirements,
        "shard": shard,
        "shard_done": shard.with_suffix(shard.suffix + ".done"),
        "shard_manifest": shard_manifest,
        "snapshot": snapshot,
    }


def _valid_version_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {"ok": True, "python": "3.12.10", "packages": {}, "mismatches": []}


def _qualification_record(root: Path, home: Path, backend_name: str) -> dict[str, object]:
    binding = airllm_runtime.current_qualification_binding(root, home=home)
    resident = backend_name == "transformers-resident"
    smoke_spec = airllm_runtime.qualification_smoke_spec(backend_name)
    return {
        "schema_version": airllm_runtime.QUALIFICATION_RECORD_SCHEMA_VERSION,
        "attempt_status": "passed",
        "passed": True,
        "hashes_verified": True,
        "model_id": MODEL_ID,
        "model_revision": REVISION,
        "architecture": "Qwen3_5ForConditionalGeneration",
        "backend_kind": "real",
        "backend": smoke_spec.backend,
        "backend_name": backend_name,
        "implementation_class": (
            "transformers.models.qwen3_5.modeling_qwen3_5.Qwen3_5ForCausalLM"
            if resident
            else "airllm.airllm_qwen3_5.AirLLMQwen3_5"
        ),
        "context_limit": 8_192 if resident else 32_768,
        "max_new_tokens": smoke_spec.max_new_tokens,
        "prompt_tokens": 16,
        "completion_tokens": 4,
        "prompt_hash": smoke_spec.prompt_hash,
        "response_hash": smoke_spec.response_hash,
        "seed": smoke_spec.seed,
        "worker_pid": 12_345,
        "worker_session_id": "c" * 32,
        "gpu_uuid": binding["gpu_uuid"],
        "startup_peak_vram_mib": 1_024.0,
        "peak_vram_mib": 2_048.0,
        "elapsed_seconds": 2.0,
        "started_at": "2026-09-05T10:00:00Z",
        "ended_at": "2026-09-05T10:00:02Z",
        "created_at": "2026-09-05T10:00:03Z",
        "qualification_binding": binding,
        "qualification_binding_sha256": airllm_runtime.qualification_binding_sha256(binding),
        "model_lock_sha256": binding["model_lock_sha256"],
        "runtime_lock_sha256": binding["runtime_lock_sha256"],
        "snapshot_manifest_sha256": binding["snapshot_manifest_sha256"],
        "shard_manifest_sha256": binding["shard_manifest_sha256"],
    }


def _evidence_details(check: str) -> dict[str, bool]:
    return {
        field: True
        for field in {
            "lifecycle_cleanup": (
                "worker_started",
                "worker_closed",
                "no_orphan_processes",
                "listener_closed",
                "gpu_state_recovered",
            ),
            "failure_fallback": (
                "worker_crash_fallback",
                "timeout_fallback",
                "malformed_output_fallback",
                "oom_fallback",
                "cleanup_after_each",
                "fallback_model_absent_before_worker",
                "fallback_unload_requested",
                "fallback_model_released",
                "fallback_gpu_recovered",
                "fallback_cleanup_bounded",
            ),
            "no_egress": (
                "offline_environment",
                "socket_guard_blocked",
                "no_external_connections",
            ),
        }[check]
    }


def test_hashed_continued_requirements_are_probed_at_exact_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python.exe"
    python.touch()
    lock = tmp_path / "requirements.lock.txt"
    lock.write_text(
        """--extra-index-url https://download.pytorch.org/whl/cu130
airllm==3.3.0 \\
    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
transformers==5.12.1 \\
    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
torch==2.14.0+cu130 \\
    --hash=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
accelerate==1.14.0 \\
    --hash=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
safetensors==0.8.0 \\
    --hash=sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
""",
        encoding="utf-8",
    )
    captured: dict[str, str] = {}

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        wanted = json.loads(command[-2])
        assert json.loads(command[-1]) == list(airllm_runtime.OPTIONAL_KERNEL_MODULES)
        captured.update(wanted)
        result = {
            "ok": True,
            "python": "3.12.10",
            "packages": wanted,
            "mismatches": [],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    monkeypatch.setattr(airllm_runtime.subprocess, "run", fake_run)

    result = airllm_runtime._runtime_versions(python, lock)

    assert result["ok"] is True
    assert captured == {
        "airllm": "3.3.0",
        "transformers": "5.12.1",
        "torch": "2.14.0+cu130",
        "accelerate": "1.14.0",
        "safetensors": "0.8.0",
    }


def test_setup_uses_hashed_pip_install_inside_a_serialized_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    project = root / "runtimes" / "airllm"
    project.mkdir(parents=True)
    (project / "requirements.lock.txt").write_text("locked", encoding="utf-8")
    python = airllm_runtime.runtime_python(home)
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("HF_TOKEN", "ambient-token-must-not-leak")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(airllm_runtime, "setup_plan", lambda _root: {"enough_disk": True})
    status_calls = 0

    def fake_status(_root: Path, **_kwargs: object) -> dict[str, object]:
        nonlocal status_calls
        status_calls += 1
        prepared = status_calls % 2 == 0
        return {
            "runtime_ready": prepared,
            "snapshot_ready": prepared,
            "shards_ready": prepared,
            "resident_ready": prepared,
            "offline_reload_proven": prepared,
            "native_optional_kernels": False,
        }

    monkeypatch.setattr(airllm_runtime, "status", fake_status)
    monkeypatch.setattr(
        airllm_runtime.shutil,
        "which",
        lambda name: "C:/tools/ollama.exe" if name == "ollama" else None,
    )
    monkeypatch.setattr(airllm_runtime, "_runtime_versions", _valid_version_probe)
    monkeypatch.setattr(
        airllm_runtime,
        "default_gpu_lease_path",
        lambda: home / "gpu.lease",
    )

    guard = threading.Lock()
    start = threading.Barrier(2)
    active = 0
    max_active = 0
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> None:
        nonlocal active, max_active
        assert active_resource_kind(root) == "build"
        assert (home / "setup.lease").is_file()
        if "prepare" in command:
            assert (home / "gpu.lease").is_file()
        if "pip" in command or "prepare" in command:
            environment = _kwargs.get("env")
            assert isinstance(environment, dict)
            assert "OPENAI_API_KEY" not in environment
            assert "HF_TOKEN" not in environment
            assert environment["CUDA_VISIBLE_DEVICES"] == "0"
        with guard:
            commands.append(command)
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.025)
        with guard:
            active -= 1

    monkeypatch.setattr(airllm_runtime, "_run", fake_run)

    def fake_stop(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["C:/tools/ollama.exe", "stop", "cyntox:latest"]
        assert (home / "gpu.lease").is_file()
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(airllm_runtime.subprocess, "run", fake_stop)

    def invoke_setup() -> dict[str, Any]:
        start.wait(timeout=2)
        return airllm_runtime.setup(root)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: invoke_setup(), range(2)))

    assert len(results) == 2
    assert max_active == 1
    pip_commands = [command for command in commands if command[1:4] == ["-m", "pip", "install"]]
    assert len(pip_commands) == 2
    assert all("--require-hashes" in command for command in pip_commands)
    assert all(
        command[command.index("--requirement") + 1] == str(project / "requirements.lock.txt")
        for command in pip_commands
    )
    assert status_calls == 4
    assert not (home / "setup.lease").exists()
    assert active_resource_kind(root) is None


def test_setup_redownloads_fresh_snapshot_when_integrity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    project = root / "runtimes" / "airllm"
    project.mkdir(parents=True)
    (project / "requirements.lock.txt").write_text("locked", encoding="utf-8")
    python = airllm_runtime.runtime_python(home)
    python.parent.mkdir(parents=True)
    python.touch()
    poisoned = home / "model" / "model.safetensors"
    poisoned.parent.mkdir(parents=True)
    poisoned.write_bytes(b"poisoned")
    (home / "snapshot-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setattr(airllm_runtime, "setup_plan", lambda _root: {"enough_disk": True})
    monkeypatch.setattr(airllm_runtime, "_runtime_versions", _valid_version_probe)
    monkeypatch.setattr(airllm_runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(airllm_runtime, "default_gpu_lease_path", lambda: home / "gpu.lease")
    calls = 0
    commands: list[list[str]] = []

    def fake_status(_root: Path, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        prepared = calls == 2
        return {
            "runtime_ready": True,
            "snapshot_ready": prepared,
            "snapshot_integrity_valid": prepared,
            "shards_ready": prepared,
            "resident_ready": prepared,
            "offline_reload_proven": prepared,
            "native_optional_kernels": False,
        }

    def fake_run(command: list[str], **_kwargs: object) -> None:
        commands.append(command)
        if "prepare" in command:
            assert "--fresh-snapshot" in command
            (home / "model").mkdir(parents=True, exist_ok=True)
            (home / "model" / "model.safetensors").write_bytes(b"fresh")

    monkeypatch.setattr(airllm_runtime, "status", fake_status)
    monkeypatch.setattr(airllm_runtime, "_run", fake_run)

    result = airllm_runtime._setup_under_activity(root)

    assert result["snapshot_integrity_valid"] is True
    assert (home / "model" / "model.safetensors").read_bytes() == b"fresh"
    assert not list(home.glob(".*-quarantine-*"))


def test_setup_restores_quarantined_snapshot_when_fresh_prepare_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    project = root / "runtimes" / "airllm"
    project.mkdir(parents=True)
    (project / "requirements.lock.txt").write_text("locked", encoding="utf-8")
    python = airllm_runtime.runtime_python(home)
    python.parent.mkdir(parents=True)
    python.touch()
    poisoned = home / "model" / "model.safetensors"
    poisoned.parent.mkdir(parents=True)
    poisoned.write_bytes(b"poisoned")
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setattr(airllm_runtime, "setup_plan", lambda _root: {"enough_disk": True})
    monkeypatch.setattr(
        airllm_runtime,
        "status",
        lambda *_args, **_kwargs: {
            "runtime_ready": True,
            "snapshot_ready": False,
            "snapshot_integrity_valid": False,
            "shards_ready": False,
            "resident_ready": False,
            "offline_reload_proven": False,
            "native_optional_kernels": False,
        },
    )
    monkeypatch.setattr(airllm_runtime, "_runtime_versions", _valid_version_probe)
    monkeypatch.setattr(airllm_runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(airllm_runtime, "default_gpu_lease_path", lambda: home / "gpu.lease")

    def fake_run(command: list[str], **_kwargs: object) -> None:
        if "prepare" in command:
            assert "--fresh-snapshot" in command
            (home / "model").mkdir(parents=True, exist_ok=True)
            (home / "model" / "model.safetensors").write_bytes(b"partial")
            raise RuntimeError("prepare failed")

    monkeypatch.setattr(airllm_runtime, "_run", fake_run)

    with pytest.raises(RuntimeError, match="prepare failed"):
        airllm_runtime._setup_under_activity(root)

    assert (home / "model" / "model.safetensors").read_bytes() == b"poisoned"
    assert not list(home.glob(".*-quarantine-*"))


def test_setup_and_cli_fail_when_post_prepare_status_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_project_locks(root)
    python = airllm_runtime.runtime_python(home)
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setattr(airllm_runtime, "setup_plan", lambda _root: {"enough_disk": True})
    monkeypatch.setattr(
        airllm_runtime,
        "status",
        lambda *_args, **_kwargs: {
            "runtime_ready": True,
            "snapshot_ready": False,
            "shards_ready": False,
            "resident_ready": False,
            "offline_reload_proven": False,
            "native_optional_kernels": False,
        },
    )
    monkeypatch.setattr(airllm_runtime, "_runtime_versions", _valid_version_probe)
    monkeypatch.setattr(airllm_runtime, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(airllm_runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(airllm_runtime, "default_gpu_lease_path", lambda: home / "gpu.lease")

    with pytest.raises(RuntimeError, match="fully verified runtime"):
        airllm_runtime._setup_under_activity(root)

    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "setup",
        lambda _root: {
            "runtime_ready": True,
            "snapshot_ready": False,
            "shards_ready": False,
            "resident_ready": False,
            "offline_reload_proven": False,
            "native_optional_kernels": False,
        },
    )
    assert cyntox_cli.cmd_model(root, ["airllm", "setup"]) == 1


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-snapshot-manifest",
        "missing-snapshot-file",
        "tampered-snapshot-size",
        "tampered-snapshot-hash-same-size",
        "underfilled-snapshot-inventory",
        "extra-snapshot-file",
        "missing-shard-manifest",
        "missing-shard-file",
        "extra-shard-file",
        "missing-shard-done-marker",
        "extra-shard-done-marker",
        "tampered-shard-size",
        "missing-offline-proof",
    ],
)
def test_status_fails_closed_for_incomplete_or_tampered_model_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    paths = _write_valid_runtime(root, home)
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setattr(airllm_runtime, "_runtime_versions", _valid_version_probe)

    if corruption == "missing-snapshot-manifest":
        paths["manifest"].unlink()
    elif corruption == "missing-snapshot-file":
        paths["snapshot"].unlink()
    elif corruption == "tampered-snapshot-size":
        paths["snapshot"].write_bytes(b"snapshot plus an unrecorded byte")
    elif corruption == "tampered-snapshot-hash-same-size":
        paths["snapshot"].write_bytes(b"Snapshot")
    elif corruption == "underfilled-snapshot-inventory":
        paths["snapshot"].write_bytes(b"short")
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest["files"][0]["size"] = paths["snapshot"].stat().st_size
        manifest["files"][0]["sha256"] = airllm_runtime.manifest_hash(paths["snapshot"])
        _write_json(paths["manifest"], manifest)
    elif corruption == "extra-snapshot-file":
        (paths["snapshot"].parent / "UNLISTED.bin").write_bytes(b"untrusted")
    elif corruption == "missing-shard-manifest":
        paths["shard_manifest"].unlink()
    elif corruption == "missing-shard-file":
        paths["shard"].unlink()
    elif corruption == "extra-shard-file":
        (paths["shard"].parent / "unlisted.safetensors").write_bytes(b"untrusted")
    elif corruption == "missing-shard-done-marker":
        paths["shard_done"].unlink()
    elif corruption == "extra-shard-done-marker":
        (paths["shard"].parent / "unlisted.safetensors.done").touch()
    elif corruption == "tampered-shard-size":
        paths["shard"].write_bytes(b"shard plus an unrecorded byte")
    else:
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest["offline_reload_proven"] = False
        _write_json(paths["manifest"], manifest)

    result = airllm_runtime.status(root, verify_hashes=True)

    if corruption == "missing-offline-proof":
        assert result["snapshot_ready"] is True
    else:
        assert result["snapshot_ready"] is False
    assert result["offline_reload_proven"] is False
    if "shard" in corruption:
        assert result["shards_ready"] is False


@pytest.mark.parametrize(
    "version_result",
    [
        {
            "ok": False,
            "python": "3.12.10",
            "mismatches": [{"package": "airllm", "expected": "3.3.0", "actual": "3.3.1"}],
        },
        {"ok": False, "python": "3.11.9", "mismatches": []},
    ],
    ids=["package-mismatch", "python-mismatch"],
)
def test_status_rejects_runtime_package_and_python_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_result: dict[str, object],
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setattr(
        airllm_runtime,
        "_runtime_versions",
        lambda *_args, **_kwargs: version_result,
    )

    result = airllm_runtime.status(root, verify_hashes=True)

    assert result["runtime_ready"] is False
    assert result["runtime_versions"] == version_result


def test_status_accepts_only_backend_specific_qualified_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setattr(airllm_runtime, "_runtime_versions", _valid_version_probe)
    _write_json(home / "qualification.json", _qualification_record(root, home, "airllm"))
    _write_json(
        home / "qualification-resident.json",
        _qualification_record(root, home, "transformers-resident"),
    )

    result = airllm_runtime.status(root)

    assert result["qualification_valid"] is True
    assert result["resident_qualification_valid"] is True

    tampered = json.loads((home / "qualification-resident.json").read_text(encoding="utf-8"))
    tampered["implementation_class"] = "example.WrongModel"
    _write_json(home / "qualification-resident.json", tampered)

    result = airllm_runtime.status(root)
    assert result["qualification_valid"] is True
    assert result["resident_qualification_valid"] is False


def test_status_reports_qualification_and_only_requires_it_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "home": str(tmp_path / "runtime"),
        "runtime_ready": True,
        "snapshot_ready": True,
        "offline_reload_proven": True,
        "shards_ready": True,
        "resident_ready": True,
        "qualification_valid": False,
        "qualification_evidence_valid": False,
        "native_optional_kernels": False,
    }

    verify_calls: list[bool] = []

    def fake_status(_root: Path, *, verify_hashes: bool = False) -> dict[str, object]:
        verify_calls.append(verify_hashes)
        return payload

    monkeypatch.setattr(cyntox_cli.airllm_runtime, "status", fake_status)

    assert cyntox_cli.cmd_model(tmp_path, ["airllm", "status"]) == 0
    output = capsys.readouterr().out
    assert "AirLLM qualification valid: False" in output
    assert "AirLLM qualification evidence valid: False" in output
    assert "Resident diagnostic ready: True" in output
    assert cyntox_cli.cmd_model(tmp_path, ["airllm", "status", "--require-qualified"]) == 1

    payload["qualification_valid"] = True
    payload["qualification_evidence_valid"] = True
    assert cyntox_cli.cmd_model(tmp_path, ["airllm", "status", "--require-qualified"]) == 0
    assert verify_calls == [False, True, True]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("attempt_status", "running"),
        ("context_limit", 1_000_000),
        ("max_new_tokens", 2_049),
        ("worker_session_id", "not-a-session"),
        ("runtime_lock_sha256", "0" * 64),
        ("snapshot_manifest_sha256", "0" * 64),
        ("shard_manifest_sha256", "0" * 64),
        ("elapsed_seconds", 901.0),
        ("ended_at", "2026-09-05T09:59:59Z"),
    ],
)
def test_qualification_record_rejects_invalid_binding_budget_session_and_timing(
    tmp_path: Path,
    field: str,
    invalid: object,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    binding = airllm_runtime.current_qualification_binding(root, home=home)
    record = _qualification_record(root, home, "airllm")
    record[field] = invalid

    assert not airllm_runtime.valid_qualification_record(
        record,
        binding=binding,
        backend_name="airllm",
    )


def test_qualification_is_invalid_after_runtime_lock_changes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    paths = _write_valid_runtime(root, home)
    record = _qualification_record(root, home, "airllm")
    paths["requirements"].write_text("new locked runtime", encoding="utf-8")
    current = airllm_runtime.current_qualification_binding(root, home=home)

    assert not airllm_runtime.valid_qualification_record(
        record,
        binding=current,
        backend_name="airllm",
    )


def test_qualification_binding_v2_deterministically_hashes_integration_surface(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)

    first = airllm_runtime.current_qualification_binding(root, home=home)
    second = airllm_runtime.current_qualification_binding(root, home=home)
    expected_names = {
        name for name, _relative_path in airllm_runtime.QUALIFICATION_IMPLEMENTATION_PATHS
    }

    assert first == second
    assert first["schema_version"] == airllm_runtime.QUALIFICATION_BINDING_SCHEMA_VERSION == 3
    assert set(first["implementation_sha256"]) == expected_names
    assert airllm_runtime.qualification_binding_is_complete(first)
    for name, relative_path in airllm_runtime.QUALIFICATION_IMPLEMENTATION_PATHS:
        assert first["implementation_sha256"][name] == airllm_runtime.manifest_hash(
            root / relative_path
        )


@pytest.mark.parametrize(
    ("name", "relative_path"), airllm_runtime.QUALIFICATION_IMPLEMENTATION_PATHS
)
def test_qualification_is_invalid_after_bound_implementation_changes(
    tmp_path: Path,
    name: str,
    relative_path: str,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    record = _qualification_record(root, home, "airllm")
    original = record["qualification_binding"]
    assert isinstance(original, dict)
    original_implementation = original["implementation_sha256"]
    assert isinstance(original_implementation, dict)

    implementation_path = root / relative_path
    implementation_path.write_bytes(
        implementation_path.read_bytes() + b"\n# implementation drift\n"
    )
    current = airllm_runtime.current_qualification_binding(root, home=home)

    assert current["implementation_sha256"][name] != original_implementation[name]
    assert not airllm_runtime.valid_qualification_record(
        record,
        binding=current,
        backend_name="airllm",
    )


@pytest.mark.parametrize("mutation", ["legacy-schema", "missing-entry", "extra-entry"])
def test_qualification_binding_shape_fails_closed(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    binding = airllm_runtime.current_qualification_binding(root, home=home)

    if mutation == "legacy-schema":
        binding["schema_version"] = 1
    elif mutation == "missing-entry":
        binding["implementation_sha256"].pop("airllm_worker")
    else:
        binding["implementation_sha256"]["unrecognized"] = "f" * 64

    assert not airllm_runtime.qualification_binding_is_complete(binding)


def test_status_fails_closed_when_bound_implementation_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    _write_json(home / "qualification.json", _qualification_record(root, home, "airllm"))
    missing = root / "oslab" / "airllm_protocol.py"
    missing.unlink()
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(home))
    monkeypatch.setattr(airllm_runtime, "_runtime_versions", _valid_version_probe)

    result = airllm_runtime.status(root)

    assert (
        result["qualification_binding"]["implementation_sha256"]["worker_protocol_parser"] is None
    )
    assert result["qualification_valid"] is False
    assert result["resident_qualification_valid"] is False


def test_three_live_distinct_repetitions_are_required_for_every_evidence_check(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    for check_index, check in enumerate(airllm_runtime.QUALIFICATION_CHECKS):
        for repetition in range(3):
            airllm_runtime.record_qualification_evidence(
                root,
                check=check,
                backend_name="airllm",
                passed=True,
                live=True,
                hashes_verified=True,
                elapsed_seconds=1.0,
                worker_session_ids=[f"{check_index * 3 + repetition + 1:032x}"],
                details=_evidence_details(check),
                run_id=f"qualification-{repetition}",
                home=home,
            )

    evidence = airllm_runtime.qualification_evidence_status(
        root,
        backend_name="airllm",
        home=home,
    )

    assert evidence["valid"] is True
    assert evidence["complete_repetitions"] == 3
    assert all(item["valid_repetitions"] == 3 for item in evidence["checks"].values())


def test_disjoint_partial_qualification_runs_cannot_accumulate_into_a_pass(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    for check_index, check in enumerate(airllm_runtime.QUALIFICATION_CHECKS):
        for repetition in range(3):
            airllm_runtime.record_qualification_evidence(
                root,
                check=check,
                backend_name="airllm",
                passed=True,
                live=True,
                hashes_verified=True,
                elapsed_seconds=1.0,
                worker_session_ids=[f"{check_index * 3 + repetition + 1:032x}"],
                details=_evidence_details(check),
                run_id=f"{check}-{repetition}",
                home=home,
            )

    evidence = airllm_runtime.qualification_evidence_status(
        root,
        backend_name="airllm",
        home=home,
    )

    assert all(item["passed"] is True for item in evidence["checks"].values())
    assert evidence["complete_repetitions"] == 0
    assert evidence["valid"] is False


def test_newer_failed_live_evidence_attempt_invalidates_older_successes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    check = "lifecycle_cleanup"
    for repetition in range(3):
        airllm_runtime.record_qualification_evidence(
            root,
            check=check,
            backend_name="airllm",
            passed=True,
            live=True,
            hashes_verified=True,
            elapsed_seconds=1.0,
            worker_session_ids=[f"{repetition + 1:032x}"],
            details=_evidence_details(check),
            run_id=f"passing-{repetition}",
            home=home,
        )
    airllm_runtime.record_qualification_evidence(
        root,
        check=check,
        backend_name="airllm",
        passed=False,
        live=True,
        hashes_verified=True,
        elapsed_seconds=1.0,
        worker_session_ids=[f"{99:032x}"],
        details=_evidence_details(check),
        run_id="newest-failure",
        home=home,
    )

    evidence = airllm_runtime.qualification_evidence_status(
        root,
        backend_name="airllm",
        home=home,
    )

    assert evidence["checks"][check]["valid_repetitions"] == 3
    assert evidence["checks"][check]["latest_attempt_passed"] is False
    assert evidence["checks"][check]["passed"] is False


def test_qualification_evidence_is_invalid_after_manifest_binding_changes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    paths = _write_valid_runtime(root, home)
    for check_index, check in enumerate(airllm_runtime.QUALIFICATION_CHECKS):
        for repetition in range(3):
            airllm_runtime.record_qualification_evidence(
                root,
                check=check,
                backend_name="airllm",
                passed=True,
                live=True,
                hashes_verified=True,
                elapsed_seconds=1.0,
                worker_session_ids=[f"{check_index * 3 + repetition + 1:032x}"],
                details=_evidence_details(check),
                run_id=f"bound-{repetition}",
                home=home,
            )
    assert airllm_runtime.qualification_evidence_status(root, backend_name="airllm", home=home)[
        "valid"
    ]

    paths["shard_manifest"].write_text('{"changed": true}', encoding="utf-8")
    evidence = airllm_runtime.qualification_evidence_status(
        root,
        backend_name="airllm",
        home=home,
    )

    assert evidence["file_valid"] is False
    assert evidence["valid"] is False


def test_unit_only_evidence_is_persisted_but_cannot_qualify(tmp_path: Path) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    check = "no_egress"
    for repetition in range(3):
        airllm_runtime.record_qualification_evidence(
            root,
            check=check,
            backend_name="airllm",
            passed=True,
            live=False,
            hashes_verified=True,
            elapsed_seconds=1.0,
            worker_session_ids=[f"{repetition + 1:032x}"],
            details=_evidence_details(check),
            run_id=f"unit-{repetition}",
            home=home,
        )

    evidence = airllm_runtime.qualification_evidence_status(
        root,
        backend_name="airllm",
        home=home,
    )

    assert evidence["record_count"] == 3
    assert evidence["valid_record_count"] == 0
    assert evidence["valid"] is False


def test_smoke_persists_pending_then_failure_and_invalidates_old_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    binding = airllm_runtime.current_qualification_binding(root, home=home)
    qualification_path = home / "qualification.json"
    _write_json(qualification_path, _qualification_record(root, home, "airllm"))
    observed_pending: list[dict[str, object]] = []
    lease_events: list[str] = []

    class RecordingAdmission:
        def __init__(self, observed_root: Path) -> None:
            assert observed_root == root

        def __enter__(self) -> None:
            lease_events.append("admission-enter")

        def __exit__(self, *_args: object) -> None:
            lease_events.append("admission-exit")

    class RecordingGpuLease:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            assert lease_events == ["admission-enter"]

        def __enter__(self) -> None:
            lease_events.append("gpu-enter")

        def __exit__(self, *_args: object) -> None:
            lease_events.append("gpu-exit")

    class FailingProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            assert lease_events == ["admission-enter", "gpu-enter"]
            lease_events.append("provider-start")
            pending = json.loads(qualification_path.read_text(encoding="utf-8"))
            assert pending["attempt_status"] == "running"
            assert pending["passed"] is False
            observed_pending.append(pending)

        async def complete(self, *_args: object, **_kwargs: object) -> object:
            raise TimeoutError("simulated transport deadline")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cyntox_cli.airllm_runtime, "runtime_home", lambda: home)
    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "current_qualification_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(cyntox_cli, "AirLlmProvider", FailingProvider)
    monkeypatch.setattr(cyntox_cli, "AirLlmAdmissionLease", RecordingAdmission)
    monkeypatch.setattr(cyntox_cli, "GpuLease", RecordingGpuLease)
    monkeypatch.setattr(cyntox_cli, "default_gpu_lease_path", lambda: home / "gpu.lease")

    exit_code = cyntox_cli.cmd_model(root, ["airllm", "smoke"])
    failure = json.loads(qualification_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert observed_pending
    assert failure["attempt_id"] == observed_pending[0]["attempt_id"]
    assert failure["seed"] == airllm_runtime.AIRLLM_SMOKE_SEED
    assert failure["attempt_status"] == "failed"
    assert failure["failure_type"] == "TimeoutError"
    assert failure["passed"] is False
    assert lease_events == [
        "admission-enter",
        "gpu-enter",
        "provider-start",
        "gpu-exit",
        "admission-exit",
    ]
    assert not airllm_runtime.valid_qualification_record(
        failure,
        binding=binding,
        backend_name="airllm",
    )


def test_qualification_acquires_resource_admission_before_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    events: list[str] = []

    class RecordingAdmission:
        def __init__(self, observed_root: Path) -> None:
            assert observed_root == root

        def __enter__(self) -> None:
            events.append("admission-enter")

        def __exit__(self, *_args: object) -> None:
            events.append("admission-exit")

    class RecordingGpuLease:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            assert events == ["admission-enter"]

        def __enter__(self) -> None:
            events.append("gpu-enter")

        def __exit__(self, *_args: object) -> None:
            events.append("gpu-exit")

    def fake_qualification(
        observed_root: Path, *, backend: str, repetitions: int
    ) -> dict[str, object]:
        assert observed_root == root
        assert backend == "airllm"
        assert repetitions == 1
        assert events == ["admission-enter", "gpu-enter"]
        events.append("qualification")
        return {"passed": True}

    monkeypatch.setattr(cyntox_cli, "AirLlmAdmissionLease", RecordingAdmission)
    monkeypatch.setattr(cyntox_cli, "GpuLease", RecordingGpuLease)
    monkeypatch.setattr(airllm_qualification, "run_qualification", fake_qualification)

    assert cyntox_cli.cmd_model(root, ["airllm", "qualify", "--repetitions", "1"]) == 0
    assert events == [
        "admission-enter",
        "gpu-enter",
        "qualification",
        "gpu-exit",
        "admission-exit",
    ]


def test_resident_smoke_uses_full_reasoning_budget_without_prompting_for_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    home = tmp_path / "runtime"
    _write_valid_runtime(root, home)
    binding = airllm_runtime.current_qualification_binding(root, home=home)
    captured: dict[str, Any] = {}

    class FailingProvider:
        last_metadata: dict[str, object] = {}

        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured["provider_kwargs"] = kwargs

        async def complete(
            self,
            messages: list[dict[str, str]],
            **kwargs: object,
        ) -> object:
            captured["messages"] = messages
            captured["completion_kwargs"] = kwargs
            raise TimeoutError("stop before launching a live model")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cyntox_cli.airllm_runtime, "runtime_home", lambda: home)
    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "current_qualification_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(cyntox_cli, "AirLlmProvider", FailingProvider)
    monkeypatch.setattr(cyntox_cli, "default_gpu_lease_path", lambda: home / "gpu.lease")

    exit_code = cyntox_cli.cmd_model(root, ["airllm", "smoke", "--backend", "resident"])

    assert exit_code == 1
    smoke_spec = airllm_runtime.qualification_smoke_spec("transformers-resident")
    assert smoke_spec.max_new_tokens == 2_048
    assert captured["provider_kwargs"] == {
        "max_new_tokens": smoke_spec.max_new_tokens,
        "backend": "resident",
        "manage_gpu_lease": False,
    }
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages == [{"role": "user", "content": "Reply with exactly: Qwythos resident ready"}]
    assert "think" not in messages[0]["content"].casefold()
    completion_kwargs = captured["completion_kwargs"]
    assert isinstance(completion_kwargs, dict)
    assert completion_kwargs["seed"] == smoke_spec.seed


@pytest.mark.parametrize(
    ("field", "drifted_value"),
    [
        ("model_id", "other/model"),
        ("revision", "0" * 40),
        ("precision", "fp16"),
        ("expected_snapshot_bytes", EXPECTED_SNAPSHOT_BYTES + 1),
        ("context_limit", 65_536),
        ("trust_remote_code", True),
    ],
)
def test_dedicated_model_lock_drift_is_rejected(
    tmp_path: Path, field: str, drifted_value: object
) -> None:
    root = tmp_path / "project"
    _write_project_locks(root)
    lock_path = root / "runtimes" / "airllm" / "model.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock[field] = drifted_value
    _write_json(lock_path, lock)

    with pytest.raises(ValueError, match="drifted from its dedicated model lock"):
        locked_model(root, "qwythos-airllm")
