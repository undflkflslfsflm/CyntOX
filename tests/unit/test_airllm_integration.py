# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from oslab import gpu_lease as gpu_lease_module
from oslab.airllm_protocol import InvalidAirLlmResponse, sanitize_response
from oslab.airllm_runtime import worker_environment
from oslab.gpu_lease import GpuLease, default_gpu_lease_path
from oslab.model.airllm import AirLlmProvider
from oslab.model_registry import (
    configured_default_profile,
    locked_model,
    provider_for_role,
    specialist_backend,
    validate_registry,
)
from scripts import cyntox_airllm_worker, cyntox_council

ROOT = Path(__file__).resolve().parents[2]


def test_worker_constants_match_the_exact_tracked_model_lock() -> None:
    model = locked_model(ROOT, "qwythos-airllm")

    assert model.model_id == cyntox_airllm_worker.MODEL_ID
    assert model.revision == cyntox_airllm_worker.REVISION
    assert model.expected_snapshot_bytes == cyntox_airllm_worker.EXPECTED_BYTES
    assert model.context_limit == cyntox_airllm_worker.CONTEXT_LIMIT
    assert model.precision == "bf16"
    assert model.trust_remote_code is False


def _hold_gpu_lease(path: str, ready: object, release: object) -> None:
    ready_event = ready
    release_event = release
    with GpuLease(Path(path), timeout=2, heartbeat=0.02):
        ready_event.set()  # type: ignore[attr-defined]
        release_event.wait(5)  # type: ignore[attr-defined]


def _read_json_retry(path: Path, *, timeout: float = 1.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.002)


def test_exact_hybrid_routing_and_legacy_default() -> None:
    assert provider_for_role(None, "critic") == "cyntox"
    assert provider_for_role("single", "fact-checker") == "cyntox"
    assert provider_for_role("hybrid-airllm", "fact-checker") == "qwythos-airllm"
    assert provider_for_role("hybrid-airllm", "critic") == "qwythos-airllm"
    assert provider_for_role("hybrid-airllm", "synthesizer") == "cyntox"
    assert provider_for_role("hybrid-airllm", "scorer", retry=True) == "cyntox"
    assert specialist_backend("hybrid-airllm") == "airllm"
    with pytest.raises(ValueError, match="Unknown model profile"):
        provider_for_role("hybrid-qwythos", "fact-checker")
    with pytest.raises(ValueError, match="Unknown model profile"):
        specialist_backend("hybrid-qwythos")
    validate_registry(ROOT)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace(
            'model_profile = "single"',
            'model_profile = "hybrid-airllm"',
            1,
        ),
        lambda text: text.replace(
            '[profiles.single]\ndefault_provider = "cyntox"',
            '[profiles.single]\ndefault_provider = "qwythos-airllm"',
        ),
        lambda text: text.replace(
            'critic = "qwythos-airllm"\nspecialist_backend = "airllm"',
            'critic = "qwythos-airllm"\nsynthesizer = "qwythos-airllm"\n'
            'specialist_backend = "airllm"',
        ),
        lambda text: text.replace(
            'fact-checker = "qwythos-airllm"',
            'fact-checker = "cyntox"',
        ),
        lambda text: text
        + '\n[models.unpinned]\nprovider = "airllm"\nmodel_id = "arbitrary/model"\n',
    ],
    ids=[
        "tracked-default",
        "single-default",
        "synthesizer-override",
        "specialist-role",
        "extra-model",
    ],
)
def test_registry_routing_policy_fails_closed_on_tracked_config_drift(
    tmp_path: Path, mutate: object
) -> None:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "runtimes" / "airllm").mkdir(parents=True)
    registry = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert callable(mutate)
    (root / "config" / "models.toml").write_text(mutate(registry), encoding="utf-8")
    shutil.copy2(
        ROOT / "runtimes" / "airllm" / "model.lock.json",
        root / "runtimes" / "airllm" / "model.lock.json",
    )

    with pytest.raises(ValueError, match="default|drifted|locked provider set"):
        validate_registry(root)


def test_registry_fails_closed_when_dedicated_model_lock_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config" / "models.toml").write_text(
        (ROOT / "config" / "models.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dedicated model lock is missing"):
        validate_registry(root)


def test_registry_rejects_coordinated_model_and_lock_drift(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "runtimes" / "airllm").mkdir(parents=True)
    registry = (
        (ROOT / "config" / "models.toml")
        .read_text(encoding="utf-8")
        .replace(
            "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated",
            "attacker/other-model",
        )
    )
    (root / "config" / "models.toml").write_text(registry, encoding="utf-8")
    lock = json.loads((ROOT / "runtimes" / "airllm" / "model.lock.json").read_text())
    lock["model_id"] = "attacker/other-model"
    (root / "runtimes" / "airllm" / "model.lock.json").write_text(
        json.dumps(lock), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="exact pinned model"):
        validate_registry(root)


def test_explicit_single_route_survives_missing_optional_airllm_lock(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config" / "models.toml").write_text(
        (ROOT / "config" / "models.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    assert provider_for_role("single", "architect", root=root) == "cyntox"
    with pytest.raises(ValueError, match="dedicated model lock is missing"):
        provider_for_role("hybrid-airllm", "fact-checker", root=root)


def test_tracked_hybrid_default_drift_fails_closed_to_single(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "runtimes" / "airllm").mkdir(parents=True)
    registry = (
        (ROOT / "config" / "models.toml")
        .read_text(encoding="utf-8")
        .replace('model_profile = "single"', 'model_profile = "hybrid-airllm"')
    )
    (root / "config" / "models.toml").write_text(registry, encoding="utf-8")
    shutil.copy2(
        ROOT / "runtimes" / "airllm" / "model.lock.json",
        root / "runtimes" / "airllm" / "model.lock.json",
    )

    assert configured_default_profile(root) == "single"


def test_worker_environment_drops_parent_secrets_and_fake_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("CYNTOX_AIRLLM_FAKE", "1")
    monkeypatch.setenv("CYNTOX_AIRLLM_FAKE_MODE", "malformed")
    monkeypatch.setenv("HF_TOKEN", "secret-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-sentinel")
    env = worker_environment(ROOT)
    assert "CYNTOX_AIRLLM_FAKE" not in env
    assert "CYNTOX_AIRLLM_FAKE_MODE" not in env
    assert "HF_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["HF_HUB_OFFLINE"] == "1"


def test_resident_shard_selection_and_parameter_mapping() -> None:
    assert cyntox_airllm_worker.is_resident_text_shard(
        Path("model.language_model.layers.0.safetensors")
    )
    assert cyntox_airllm_worker.is_resident_text_shard(Path("lm_head.safetensors"))
    assert not cyntox_airllm_worker.is_resident_text_shard(Path("model.visual.safetensors"))
    assert (
        cyntox_airllm_worker.resident_parameter_name(
            "model.language_model.layers.3.mlp.down_proj.weight"
        )
        == "model.layers.3.mlp.down_proj.weight"
    )
    assert cyntox_airllm_worker.resident_parameter_name("lm_head.weight") == "lm_head.weight"


def test_reasoning_is_removed_and_invalid_responses_are_rejected() -> None:
    assert sanitize_response("<think>secret</think>Visible") == "Visible"
    with pytest.raises(InvalidAirLlmResponse, match="incomplete"):
        sanitize_response("<think>secret Visible")
    with pytest.raises(InvalidAirLlmResponse, match="empty"):
        sanitize_response("<think>secret</think>")
    with pytest.raises(InvalidAirLlmResponse, match="repetition"):
        sanitize_response(" ".join(["a b c d"] * 4))
    with pytest.raises(InvalidAirLlmResponse, match="repetition"):
        sanitize_response(" ".join(["one two three"] * 4))


def test_worker_and_provider_strip_only_well_formed_reasoning_spans() -> None:
    response = "Before<think>first secret</think>Middle<think>second secret</think>After"
    assert sanitize_response(response) == "BeforeMiddleAfter"
    assert cyntox_airllm_worker._sanitize_generated_text(response) == "BeforeMiddleAfter"


@pytest.mark.parametrize(
    "response",
    [
        "</think>Visible<think>secret</think>",
        "<think>outer<think>nested</think></think>Visible",
        "<think >secret</think >Visible",
        "<THINK>secret</THINK>Visible",
        "<thinking>secret</thinking>Visible",
        "< think>secret</ think>Visible",
        "<think>secret</think",
        "<think>secret</thin>",
        "Visible<thin",
        "Visible</thi",
    ],
    ids=[
        "reversed",
        "nested",
        "whitespace",
        "uppercase",
        "extended-name",
        "split-open",
        "partial-close",
        "short-close",
        "partial-open-fragment",
        "partial-close-fragment",
    ],
)
def test_worker_and_provider_fail_closed_on_ambiguous_reasoning_tags(response: str) -> None:
    with pytest.raises(InvalidAirLlmResponse, match="reasoning"):
        sanitize_response(response)
    with pytest.raises(ValueError, match="reasoning"):
        cyntox_airllm_worker._sanitize_generated_text(response)


def test_worker_restores_only_template_opening_and_requires_generated_close() -> None:
    complete = cyntox_airllm_worker._restore_template_think_prefix(
        "private reasoning</think>Visible"
    )
    assert cyntox_airllm_worker._sanitize_generated_text(complete) == "Visible"

    incomplete = cyntox_airllm_worker._restore_template_think_prefix("private reasoning")
    with pytest.raises(ValueError, match="incomplete reasoning span"):
        cyntox_airllm_worker._sanitize_generated_text(incomplete)


def test_worker_requires_thinking_enabled_template_prefix() -> None:
    class RecordingTokenizer:
        def __init__(self, rendered: str) -> None:
            self.rendered = rendered
            self.calls: list[dict[str, object]] = []

        def apply_chat_template(
            self,
            messages: list[dict[str, str]],
            **kwargs: object,
        ) -> str:
            self.calls.append({"messages": messages, **kwargs})
            return self.rendered

    tokenizer = RecordingTokenizer("chat prompt<think>\n")
    assert cyntox_airllm_worker._render_thinking_prompt(tokenizer, "hello") == tokenizer.rendered
    assert tokenizer.calls == [
        {
            "messages": [{"role": "user", "content": "hello"}],
            "tokenize": False,
            "add_generation_prompt": True,
            "enable_thinking": True,
        }
    ]

    missing_prefix = RecordingTokenizer("chat prompt")
    with pytest.raises(RuntimeError, match="did not open a reasoning span"):
        cyntox_airllm_worker._render_thinking_prompt(missing_prefix, "hello")


def test_structured_generation_validates_private_reasoning_and_returns_final_only() -> None:
    calls: list[tuple[str, int, bool, int | None]] = []

    def generate_once(
        prompt: str,
        token_budget: int,
        thinking: bool,
        seed: int | None,
    ) -> tuple[str, int, int]:
        calls.append((prompt, token_budget, thinking, seed))
        if thinking:
            return 'hidden reasoning</think>{"ready": "draft"}', 20, 12
        return '{"ready": true}', 30, 4

    text, prompt_tokens, completion_tokens = cyntox_airllm_worker._complete_structured_generation(
        generate_once,
        "Return readiness JSON",
        128,
        7,
    )

    assert text == '{"ready": true}'
    assert "<think>" not in text
    assert prompt_tokens == 50
    assert completion_tokens == 16
    assert calls[0] == ("Return readiness JSON", 64, True, 7)
    assert calls[1][1:] == (64, False, 8)
    assert 'Private draft (do not quote):\n{"ready": "draft"}' in calls[1][0]
    assert "hidden reasoning" not in calls[1][0]


def test_structured_generation_rejects_incomplete_private_reasoning_before_final_pass() -> None:
    calls: list[tuple[bool, int | None]] = []

    def generate_once(
        _prompt: str,
        _token_budget: int,
        thinking: bool,
        seed: int | None,
    ) -> tuple[str, int, int]:
        calls.append((thinking, seed))
        return "unterminated private reasoning", 20, 64

    with pytest.raises(ValueError, match="incomplete reasoning span"):
        cyntox_airllm_worker._complete_structured_generation(
            generate_once,
            "Return readiness JSON",
            128,
            7,
        )
    assert calls == [(True, 7)]


def test_stale_gpu_lease_is_only_recovered_for_dead_owner(tmp_path: Path) -> None:
    lease_path = tmp_path / "gpu.lease"
    lease_path.write_text(json.dumps({"pid": 2_147_483_647, "heartbeat": 0}), encoding="utf-8")
    with GpuLease(lease_path, timeout=1, heartbeat=0.05):
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
        assert payload["pid"] != 2_147_483_647
    assert not lease_path.exists()


def test_gpu_lease_records_identity_nonce_and_atomic_heartbeats(tmp_path: Path) -> None:
    lease_path = tmp_path / "gpu.lease"
    with GpuLease(lease_path, timeout=1, heartbeat=0.01):
        initial = _read_json_retry(lease_path)
        assert initial["pid"] == os.getpid()
        assert initial["process_create_time"] == pytest.approx(
            psutil.Process().create_time(), abs=0.01
        )
        assert len(initial["nonce"]) == 32

        deadline = time.monotonic() + 1
        latest = initial
        while latest["heartbeat"] <= initial["heartbeat"] and time.monotonic() < deadline:
            latest = _read_json_retry(lease_path)
            time.sleep(0.005)
        assert latest["heartbeat"] > initial["heartbeat"]
        assert latest["nonce"] == initial["nonce"]
        # Atomic replacement necessarily exposes its private staging file for a
        # brief interval. Cleanup is a post-write/post-release invariant.
    assert not lease_path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_gpu_lease_recovers_reused_pid_identity(tmp_path: Path) -> None:
    lease_path = tmp_path / "gpu.lease"
    lease_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "process_create_time": psutil.Process().create_time() - 60,
                "nonce": "stale-owner",
                "heartbeat": time.time(),
            }
        ),
        encoding="utf-8",
    )
    with GpuLease(lease_path, timeout=1, heartbeat=0.05):
        current = json.loads(lease_path.read_text(encoding="utf-8"))
        assert current["nonce"] != "stale-owner"


def test_gpu_lease_does_not_recover_matching_live_process(tmp_path: Path) -> None:
    lease_path = tmp_path / "gpu.lease"
    lease_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "process_create_time": psutil.Process().create_time(),
                "nonce": "live-owner",
                "heartbeat": time.time(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TimeoutError, match="remained busy"):
        GpuLease(lease_path, timeout=0.02, heartbeat=0.05).acquire()
    assert json.loads(lease_path.read_text(encoding="utf-8"))["nonce"] == "live-owner"


def test_gpu_lease_release_cannot_remove_replacement_with_same_pid(tmp_path: Path) -> None:
    lease_path = tmp_path / "gpu.lease"
    lease = GpuLease(lease_path, timeout=1, heartbeat=60)
    lease.acquire()
    owned = json.loads(lease_path.read_text(encoding="utf-8"))
    replacement = {**owned, "nonce": "replacement-owner"}
    lease_path.write_text(json.dumps(replacement), encoding="utf-8")
    lease.release()
    assert json.loads(lease_path.read_text(encoding="utf-8"))["nonce"] == "replacement-owner"


def test_gpu_lease_inherited_by_another_pid_cannot_release_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease_path = tmp_path / "gpu.lease"
    lease = GpuLease(lease_path, timeout=1, heartbeat=60)
    lease.acquire()
    owner_pid = json.loads(lease_path.read_text(encoding="utf-8"))["pid"]
    monkeypatch.setattr(gpu_lease_module.os, "getpid", lambda: owner_pid + 1)
    lease.release()
    assert lease_path.is_file()


def test_gpu_lease_is_exclusive_across_processes(tmp_path: Path) -> None:
    lease_path = tmp_path / "gpu.lease"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_gpu_lease, args=(str(lease_path), ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(TimeoutError, match="remained busy"):
            GpuLease(lease_path, timeout=0.05, heartbeat=0.05).acquire()
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode == 0
    with GpuLease(lease_path, timeout=1, heartbeat=0.05):
        assert lease_path.is_file()


def test_default_gpu_lease_path_is_host_wide_gpu_keyed_and_path_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gpu_lease_module, "_discover_gpu_uuid", lambda: "GPU-discovered")
    discovered = default_gpu_lease_path(base_dir=tmp_path)
    same = default_gpu_lease_path("gpu-DISCOVERED", base_dir=tmp_path)
    other = default_gpu_lease_path("GPU-other", base_dir=tmp_path)
    hostile = default_gpu_lease_path("../../outside/GPU", base_dir=tmp_path)
    assert discovered == same
    assert other != discovered
    assert discovered.parent == tmp_path.resolve()
    assert hostile.parent == tmp_path.resolve()
    assert ".." not in hostile.name


def test_default_gpu_lease_path_uses_conservative_unknown_gpu_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CYNTOX_GPU_LEASE_DIR", str(tmp_path))
    monkeypatch.setattr(gpu_lease_module, "_discover_gpu_uuid", lambda: None)
    first = default_gpu_lease_path()
    second = default_gpu_lease_path()
    assert first == second
    assert first.parent == tmp_path.resolve()


def test_gpu_uuid_discovery_respects_visible_device_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CYNTOX_GPU_UUID", raising=False)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1,0")
    monkeypatch.setattr("oslab.gpu_lease.shutil.which", lambda _name: "nvidia-smi")
    monkeypatch.setattr(
        "oslab.gpu_lease.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["nvidia-smi"], 0, "0, GPU-zero\n1, GPU-one\n", ""
        ),
    )
    assert gpu_lease_module._discover_gpu_uuid() == "GPU-one"


def test_gpu_uuid_prefix_is_canonicalized_before_lease_keying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    full_uuid = "GPU-abcdef12-3456-7890-abcd-ef1234567890"
    monkeypatch.delenv("CYNTOX_GPU_UUID", raising=False)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-abcdef12")
    monkeypatch.setattr(gpu_lease_module.shutil, "which", lambda _name: "nvidia-smi")
    monkeypatch.setattr(
        gpu_lease_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["nvidia-smi"], 0, f"0, {full_uuid}\n", ""
        ),
    )

    assert gpu_lease_module.selected_gpu_uuid() == full_uuid
    assert default_gpu_lease_path(base_dir=tmp_path) == default_gpu_lease_path(
        full_uuid, base_dir=tmp_path
    )


@pytest.mark.parametrize("_repeat", range(3))
def test_fake_worker_protocol_lifecycle_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _repeat: int
) -> None:
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(tmp_path / "runtime"))

    async def exercise() -> tuple[int, int]:
        provider = AirLlmProvider(ROOT, worker_python=Path(sys.executable), fake=True)
        await provider.start(timeout=15)
        assert provider.process is not None
        pid = provider.process.pid
        port = int(provider.port or 0)
        response = await provider.complete([{"role": "user", "content": "test"}], timeout=10)
        assert response.content.startswith("Fake Qwythos response")
        assert response.model.architecture == "Qwen3_5ForConditionalGeneration"
        assert provider.last_metadata["peak_vram_mib"] == 64.0
        structured = await provider.complete(
            [{"role": "user", "content": "return status"}],
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            timeout=10,
        )
        assert structured.structured == {"ok": True}
        await provider.close()
        assert provider.process is None
        return pid, port

    _pid, port = asyncio.run(exercise())
    with socket.socket() as client:
        client.settimeout(0.5)
        assert client.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.parametrize("mode", ["crash", "oom", "timeout", "malformed"])
def test_fake_worker_failure_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    monkeypatch.setenv("CYNTOX_AIRLLM_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("CYNTOX_AIRLLM_FAKE_MODE", mode)

    async def exercise() -> None:
        provider = AirLlmProvider(ROOT, worker_python=Path(sys.executable), fake=True)
        try:
            with pytest.raises((OSError, RuntimeError, ValueError)):
                await provider.complete(
                    [{"role": "user", "content": "exercise failure"}],
                    timeout=0.1 if mode == "timeout" else 10,
                )
        finally:
            await provider.close(force=True)

    asyncio.run(exercise())


def test_adaptive_vram_residency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stopped: list[str] = []
    monkeypatch.setattr(cyntox_council, "airllm_measured_peak_mib", lambda: 4000.0)
    monkeypatch.setattr(cyntox_council, "gpu_free_vram_mib", lambda: 9000.0)
    monkeypatch.setattr(
        cyntox_council,
        "stop_resident_ollama_model",
        lambda model, **_kwargs: (
            stopped.append(model) or {"verified": True, "performed": True, "free_vram_mib": 6000.0}
        ),
    )
    session = cyntox_council.CouncilAirLlmSession(tmp_path, ollama_model="cyntox:latest")
    session._prepare_residency("airllm", deadline=time.monotonic() + 30)
    assert not session.unloaded_ollama
    assert stopped == []
    monkeypatch.setattr(cyntox_council, "gpu_free_vram_mib", lambda: 6000.0)
    session._prepare_residency("airllm", deadline=time.monotonic() + 30)
    assert session.unloaded_ollama
    assert stopped == ["cyntox:latest"]


def test_hybrid_fallback_is_visible_in_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailedSession:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def begin(self) -> None:
            pass

        def _remaining(self) -> float:
            return 1800.0

        def acquire_admission(self) -> None:
            pass

        def complete(self, _prompt: str, **_kwargs: object) -> tuple[str, dict[str, object]]:
            raise RuntimeError("simulated worker crash")

        def close(self) -> None:
            pass

    monkeypatch.setattr(cyntox_council, "project_root", lambda: tmp_path)
    monkeypatch.setattr(cyntox_council, "CouncilAirLlmSession", FailedSession)
    monkeypatch.setattr(cyntox_council, "gpu_free_vram_mib", lambda: 12345.0)
    monkeypatch.setattr(
        cyntox_council,
        "run_role",
        lambda *_args, **_kwargs: __import__("subprocess").CompletedProcess(
            ["fake-cyntox"], 0, "CyntOX fallback output", ""
        ),
    )
    assert (
        cyntox_council.main(
            [
                "--model-profile",
                "hybrid-airllm",
                "--roles",
                "fact-checker,synthesizer",
                "--out-dir",
                "runs",
                "test fallback",
            ]
        )
        == 0
    )
    manifest_path = next((tmp_path / "runs").glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first = manifest["results"][0]
    assert manifest["status"] == "degraded"
    assert first["requested_provider"] == "qwythos-airllm"
    assert first["actual_provider"] == "cyntox"
    assert first["fallback_reason"] == "worker_failure"
    assert "WARNING: Qwythos failed" in capsys.readouterr().err
    persisted_prompt = Path(first["prompt"]).read_text(encoding="utf-8")
    assert "test fallback" not in persisted_prompt
    assert "intentionally not persisted" in persisted_prompt


def test_model_registry_is_pinned_to_full_bf16_checkpoint() -> None:
    registry = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated" in registry
    assert "efcc73cac15ff8fc5d46b8d41b53c22d571cf97d" in registry
    assert "expected_snapshot_bytes = 19333096957" in registry
    assert 'precision = "bf16"' in registry
