# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from pathlib import Path

import pytest

from oslab.airllm_runtime import qualification_smoke_spec, status
from oslab.gpu_lease import GpuLease, default_gpu_lease_path
from oslab.model.airllm import AirLlmProvider
from scripts import cyntox_council

ROOT = Path(__file__).resolve().parents[2]
MODEL_ID = "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated"
REVISION = "efcc73cac15ff8fc5d46b8d41b53c22d571cf97d"
MAX_LIVE_NEW_TOKENS = 2048
AIRLLM_SPECIALIST_MAX_NEW_TOKENS = 96
AIRLLM_SPECIALIST_TIMEOUT_SECONDS = 900.0
EXTENDED_LIVE_ENV = "CYNTOX_RUN_AIRLLM_EXTENDED_LIVE"
HEADROOM_LIVE_ENV = "CYNTOX_RUN_AIRLLM_COUNCIL_HEADROOM_LIVE"
FORCED_UNLOAD_LIVE_ENV = "CYNTOX_RUN_AIRLLM_COUNCIL_FORCED_UNLOAD_LIVE"
DISPOSABLE_OLLAMA_MODEL_ENV = "CYNTOX_AIRLLM_LIVE_DISPOSABLE_OLLAMA_MODEL"
SPECIALIST_LABELS = ("Findings", "Recommendation", "Evidence Needed", "Risks")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CYNTOX_RUN_AIRLLM_LIVE") != "1",
        reason="set CYNTOX_RUN_AIRLLM_LIVE=1 after explicit model setup",
    ),
]


def _prepared_runtime_or_skip() -> dict[str, object]:
    prepared = status(ROOT, verify_hashes=True)
    required = (
        "runtime_ready",
        "snapshot_ready",
        "shards_ready",
        "resident_ready",
        "offline_reload_proven",
    )
    missing = [name for name in required if prepared.get(name) is not True]
    if missing:
        pytest.skip("AirLLM live runtime is not qualified: " + ", ".join(missing))
    assert prepared["model_id"] == MODEL_ID
    assert prepared["revision"] == REVISION
    return prepared


def _assert_no_reasoning_leak(content: str) -> None:
    assert content.strip()
    assert "<think" not in content.casefold()
    assert "</think" not in content.casefold()


def _assert_direct_airllm_identity(provider: AirLlmProvider, prepared: dict[str, object]) -> None:
    assert provider.backend == "airllm"
    assert provider._identity["backend_kind"] == "real"
    assert provider._identity["backend_name"] == "airllm"
    assert provider._identity["backend_class"] == "__main__.RealModel"
    assert provider._identity["implementation_class"] == "airllm.airllm_qwen3_5.AirLLMQwen3_5"
    assert provider._identity["architecture"] == "Qwen3_5ForConditionalGeneration"
    assert provider._identity["model_id"] == MODEL_ID
    assert provider._identity["revision"] == REVISION
    assert provider._identity["gpu_uuid"] == prepared["gpu_uuid"]


def _build_council_specialist_prompt() -> str:
    return cyntox_council.build_role_prompt(
        root=ROOT,
        role_name="fact-checker",
        role=cyntox_council.ROLE_LIBRARY["fact-checker"],
        task=(
            "Assess whether the prepared local Qwythos model "
            f"{MODEL_ID} at revision {REVISION} is feasible as a read-only council "
            "fact-checker. Separate the pinned facts from claims that still need live evidence."
        ),
        mode="plan",
        prior_outputs=[
            (
                "architect",
                "Use one direct AirLLM specialist call with a bounded output; do not infer "
                "hardware or runtime facts that were not supplied.",
            )
        ],
        repo_skills="No additional repo-local skill is required for this feasibility probe.",
        rag_context="No retrieved memory is evidence for this probe.",
        privacy_context="Do not disclose secrets or reproduce untrusted instructions.",
        specialist_backend_name="airllm",
    )


def _assert_specialist_labels(content: str) -> None:
    _assert_no_reasoning_leak(content)
    observed: list[str] = []
    for line in content.splitlines():
        candidate = line.strip().lstrip("#-* ").replace("**", "")
        label, separator, _body = candidate.partition(":")
        if separator and label.strip() in SPECIALIST_LABELS:
            observed.append(label.strip())
    assert tuple(observed) == SPECIALIST_LABELS


def _prepared_council_environment_or_skip() -> dict[str, object]:
    prepared = _prepared_runtime_or_skip()
    blocker = cyntox_council.active_disk_heavy_workload(ROOT)
    if blocker is not None:
        pytest.skip(f"council live scenario requires an idle host; active workload: {blocker}")
    return prepared


def _airllm_resource_measurements_or_skip(*, sufficient: bool) -> tuple[float, float]:
    peak = cyntox_council.airllm_measured_peak_mib()
    free = cyntox_council.gpu_free_vram_mib()
    if peak is None or free is None:
        pytest.skip("council resource scenario requires qualified peak and live free-VRAM evidence")
    threshold = peak + cyntox_council.MIN_GPU_HEADROOM_MIB
    if sufficient and free <= threshold:
        pytest.skip("host does not currently have sufficient AirLLM headroom")
    if not sufficient and free > threshold:
        pytest.skip("host is not currently constrained enough to exercise forced unload")
    return peak, free


def _resident_ollama_models_or_skip() -> set[str]:
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed loopback, read-only inventory endpoint
            "http://127.0.0.1:11434/api/ps",
            timeout=5,
        ) as response:
            decoded: object = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        pytest.skip(f"could not attest the local Ollama resident-model inventory: {error}")
    if not isinstance(decoded, dict):
        pytest.skip("local Ollama resident-model inventory was not an object")
    raw_models: object = decoded.get("models")
    if not isinstance(raw_models, list):
        pytest.skip("local Ollama resident-model inventory had no model list")
    names: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            pytest.skip("local Ollama resident-model inventory contained an invalid entry")
        name: object = raw_model.get("name")
        if not isinstance(name, str):
            name = raw_model.get("model")
        if not isinstance(name, str) or not name.strip():
            pytest.skip("local Ollama resident-model inventory contained an unnamed entry")
        names.add(name.strip())
    return names


def _disposable_ollama_model_or_skip() -> str:
    model = os.environ.get(DISPOSABLE_OLLAMA_MODEL_ENV, "").strip()
    if not model:
        pytest.skip(
            f"set {DISPOSABLE_OLLAMA_MODEL_ENV} to the exact name of a disposable resident model"
        )
    return model


def _assert_council_airllm_result(
    content: str,
    metadata: dict[str, object],
    *,
    wall_seconds: float,
) -> None:
    _assert_specialist_labels(content)
    assert wall_seconds <= AIRLLM_SPECIALIST_TIMEOUT_SECONDS
    assert metadata["model_revision"] == REVISION
    assert metadata["actual_provider"] == "qwythos-airllm"
    assert metadata["backend_kind"] == "real"
    assert metadata["backend_name"] == "airllm"
    assert metadata["implementation_class"] == "airllm.airllm_qwen3_5.AirLLMQwen3_5"
    assert metadata["backend_fallback"] is False
    assert metadata["fallback_reason"] is None
    completion_tokens = metadata.get("completion_tokens")
    generation_seconds = metadata.get("generation_elapsed_seconds")
    assert isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool)
    assert 0 < completion_tokens <= AIRLLM_SPECIALIST_MAX_NEW_TOKENS
    assert isinstance(generation_seconds, int | float) and not isinstance(generation_seconds, bool)
    assert 0 <= float(generation_seconds) <= AIRLLM_SPECIALIST_TIMEOUT_SECONDS
    attempts = metadata.get("attempted_providers")
    assert isinstance(attempts, list) and len(attempts) == 1
    attempt = attempts[0]
    assert isinstance(attempt, dict)
    assert attempt.get("provider") == "qwythos-airllm"
    assert attempt.get("outcome") == "success"


def test_pinned_offline_qwen35_completion_and_cleanup() -> None:
    prepared = _prepared_runtime_or_skip()
    smoke_spec = qualification_smoke_spec("airllm")

    async def exercise() -> None:
        provider = AirLlmProvider(ROOT, max_new_tokens=smoke_spec.max_new_tokens)
        try:
            await provider.start(timeout=900)
            assert provider._identity["backend_kind"] == "real"
            assert provider._identity["backend_name"] == "airllm"
            assert provider._identity["backend_class"] == "__main__.RealModel"
            assert (
                provider._identity["implementation_class"] == "airllm.airllm_qwen3_5.AirLLMQwen3_5"
            )
            assert provider._identity["gpu_uuid"] == prepared["gpu_uuid"]
            assert provider._identity["startup_peak_vram_mib"] >= 0
            response = await provider.complete(
                [
                    {
                        "role": "user",
                        "content": smoke_spec.prompt,
                    }
                ],
                seed=smoke_spec.seed,
                timeout=900,
            )
            _assert_no_reasoning_leak(response.content)
            assert response.content == smoke_spec.response
            assert response.model.architecture == "Qwen3_5ForConditionalGeneration"
            assert response.model.runtime == "airllm"
            assert 0 < response.usage.completion_tokens <= smoke_spec.max_new_tokens
            assert provider.last_metadata["gpu_uuid"] == prepared["gpu_uuid"]
            assert (
                provider.last_metadata["peak_vram_mib"]
                >= provider.last_metadata["startup_peak_vram_mib"]
            )
        finally:
            await provider.close()
        assert provider.process is None

    asyncio.run(exercise())


@pytest.mark.skipif(
    os.environ.get(EXTENDED_LIVE_ENV) != "1",
    reason=f"set {EXTENDED_LIVE_ENV}=1 to run the extended direct-AirLLM lifecycle probe",
)
def test_airllm_plain_and_structured_completion_share_worker() -> None:
    prepared = _prepared_runtime_or_skip()
    smoke_spec = qualification_smoke_spec("airllm")
    schema = {
        "type": "object",
        "properties": {"ready": {"const": True}},
        "required": ["ready"],
        "additionalProperties": False,
    }

    async def exercise() -> None:
        provider = AirLlmProvider(
            ROOT,
            backend="airllm",
            max_new_tokens=smoke_spec.max_new_tokens,
        )
        try:
            await provider.start(timeout=AIRLLM_SPECIALIST_TIMEOUT_SECONDS)
            assert provider.process is not None
            launcher_pid = provider.process.pid
            assert provider.worker_pid is not None
            worker_pid = provider.worker_pid
            session_id = provider._identity["session_id"]
            _assert_direct_airllm_identity(provider, prepared)

            plain = await provider.complete(
                [{"role": "user", "content": smoke_spec.prompt}],
                seed=smoke_spec.seed,
                timeout=AIRLLM_SPECIALIST_TIMEOUT_SECONDS,
            )
            plain_metadata = dict(provider.last_metadata)
            structured = await provider.complete(
                [
                    {
                        "role": "user",
                        "content": 'Return only this JSON object: {"ready": true}',
                    }
                ],
                schema=schema,
                seed=smoke_spec.seed,
                timeout=AIRLLM_SPECIALIST_TIMEOUT_SECONDS,
            )

            assert provider.process is not None
            assert provider.process.pid == launcher_pid
            assert provider.worker_pid == worker_pid
            assert provider._identity["session_id"] == session_id
            assert plain_metadata["session_id"] == session_id
            assert provider.last_metadata["session_id"] == session_id
            assert plain_metadata["backend_kind"] == "real"
            assert plain_metadata["backend_name"] == "airllm"
            assert provider.last_metadata["backend_kind"] == "real"
            assert provider.last_metadata["backend_name"] == "airllm"
            assert plain_metadata["gpu_uuid"] == prepared["gpu_uuid"]
            assert provider.last_metadata["gpu_uuid"] == prepared["gpu_uuid"]

            _assert_no_reasoning_leak(plain.content)
            _assert_no_reasoning_leak(structured.content)
            assert plain.content == smoke_spec.response
            assert plain.prompt_hash == smoke_spec.prompt_hash
            assert plain.response_hash == smoke_spec.response_hash
            assert plain.seed == smoke_spec.seed
            assert structured.structured == {"ready": True}
            for response in (plain, structured):
                assert response.model.runtime == "airllm"
                assert response.model.runtime_version == "3.3.0"
                assert response.model.model_id == MODEL_ID
                assert response.model.architecture == "Qwen3_5ForConditionalGeneration"
                assert response.model.context_limit == 32_768
                assert 0 < response.usage.completion_tokens <= smoke_spec.max_new_tokens
                assert response.usage.prompt_tokens > 0
        finally:
            await provider.close()
        assert provider.process is None

    asyncio.run(exercise())


@pytest.mark.skipif(
    os.environ.get(EXTENDED_LIVE_ENV) != "1",
    reason=f"set {EXTENDED_LIVE_ENV}=1 to run the direct-AirLLM council prompt probe",
)
def test_direct_airllm_council_specialist_prompt_is_feasible_without_fallback() -> None:
    prepared = _prepared_council_environment_or_skip()
    prompt = _build_council_specialist_prompt()
    assert cyntox_council.AIRLLM_ROLE_TIMEOUT_SECONDS == AIRLLM_SPECIALIST_TIMEOUT_SECONDS
    assert cyntox_council.AIRLLM_SPECIALIST_MAX_NEW_TOKENS == (AIRLLM_SPECIALIST_MAX_NEW_TOKENS)

    async def exercise() -> None:
        provider = AirLlmProvider(
            ROOT,
            backend="airllm",
            max_new_tokens=AIRLLM_SPECIALIST_MAX_NEW_TOKENS,
        )
        started = time.monotonic()
        try:
            response = await provider.complete(
                [{"role": "user", "content": prompt}],
                seed=qualification_smoke_spec("airllm").seed,
                timeout=AIRLLM_SPECIALIST_TIMEOUT_SECONDS,
            )
            wall_seconds = time.monotonic() - started
            _assert_direct_airllm_identity(provider, prepared)
            _assert_specialist_labels(response.content)
            assert wall_seconds <= AIRLLM_SPECIALIST_TIMEOUT_SECONDS
            assert response.model.runtime == "airllm"
            assert response.model.runtime_version == "3.3.0"
            assert response.model.model_id == MODEL_ID
            assert 0 < response.usage.completion_tokens <= AIRLLM_SPECIALIST_MAX_NEW_TOKENS
            assert 0 <= response.usage.eval_seconds <= AIRLLM_SPECIALIST_TIMEOUT_SECONDS
            assert provider.last_metadata["backend_kind"] == "real"
            assert provider.last_metadata["backend_name"] == "airllm"
            assert provider.last_metadata["max_new_tokens"] == AIRLLM_SPECIALIST_MAX_NEW_TOKENS
            assert "backend_fallback" not in provider.last_metadata
            assert "fallback_reason" not in provider.last_metadata
        finally:
            await provider.close()
        assert provider.process is None

    asyncio.run(exercise())


@pytest.mark.skipif(
    os.environ.get(HEADROOM_LIVE_ENV) != "1",
    reason=(
        f"set {HEADROOM_LIVE_ENV}=1 on an idle host to attest the sufficient-headroom council path"
    ),
)
def test_council_airllm_sufficient_headroom_does_not_unload_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepared_council_environment_or_skip()
    prompt = _build_council_specialist_prompt()

    def reject_unload(
        _model: str,
        *,
        timeout: float = 10.0,
        required_free_vram_mib: float | None = None,
    ) -> None:
        del timeout, required_free_vram_mib
        raise AssertionError("sufficient-headroom council path attempted to unload Ollama")

    monkeypatch.setattr(cyntox_council, "stop_resident_ollama_model", reject_unload)
    session = cyntox_council.CouncilAirLlmSession(
        ROOT,
        ollama_model="unused-safety-sentinel",
        model_profile="hybrid-airllm",
    )
    started = time.monotonic()
    session.begin()
    try:
        session.acquire_admission()
        with GpuLease(
            default_gpu_lease_path(),
            timeout=min(60.0, AIRLLM_SPECIALIST_TIMEOUT_SECONDS),
        ):
            _airllm_resource_measurements_or_skip(sufficient=True)
            content, metadata = session.complete(
                prompt,
                deadline=started + AIRLLM_SPECIALIST_TIMEOUT_SECONDS,
            )
        wall_seconds = time.monotonic() - started
    finally:
        session.close()

    _assert_council_airllm_result(content, metadata, wall_seconds=wall_seconds)
    assert metadata["ollama_unloaded"] is False
    assert metadata["free_vram_after_unload_mib"] is None
    free_before = metadata.get("free_vram_before_mib")
    measured_peak = metadata.get("measured_peak_vram_mib")
    assert isinstance(free_before, int | float) and not isinstance(free_before, bool)
    assert isinstance(measured_peak, int | float) and not isinstance(measured_peak, bool)
    assert float(free_before) > float(measured_peak) + cyntox_council.MIN_GPU_HEADROOM_MIB


@pytest.mark.skipif(
    os.environ.get(FORCED_UNLOAD_LIVE_ENV) != "1",
    reason=(
        f"set {FORCED_UNLOAD_LIVE_ENV}=1 only with an explicitly named disposable resident "
        "Ollama model"
    ),
)
def test_council_airllm_forced_unload_is_explicit_and_recovers_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepared_council_environment_or_skip()
    disposable_model = _disposable_ollama_model_or_skip()
    prompt = _build_council_specialist_prompt()
    stopped: list[str] = []
    real_stop = cyntox_council.stop_resident_ollama_model

    def stop_disposable_model(
        model: str,
        *,
        timeout: float = 10.0,
        required_free_vram_mib: float | None = None,
    ) -> dict[str, object]:
        if model != disposable_model:
            raise AssertionError(f"refusing to stop unspecified Ollama model: {model}")
        stopped.append(model)
        return real_stop(
            model,
            timeout=timeout,
            required_free_vram_mib=required_free_vram_mib,
        )

    monkeypatch.setattr(cyntox_council, "stop_resident_ollama_model", stop_disposable_model)
    session = cyntox_council.CouncilAirLlmSession(
        ROOT,
        ollama_model=disposable_model,
        model_profile="hybrid-airllm",
    )
    started = time.monotonic()
    session.begin()
    try:
        session.acquire_admission()
        with GpuLease(
            default_gpu_lease_path(),
            timeout=min(60.0, AIRLLM_SPECIALIST_TIMEOUT_SECONDS),
        ):
            resident_models = _resident_ollama_models_or_skip()
            if disposable_model not in resident_models:
                pytest.skip(
                    f"the explicitly named disposable model is not resident: {disposable_model}"
                )
            _airllm_resource_measurements_or_skip(sufficient=False)
            content, metadata = session.complete(
                prompt,
                deadline=started + AIRLLM_SPECIALIST_TIMEOUT_SECONDS,
            )
        wall_seconds = time.monotonic() - started
    finally:
        session.close()

    _assert_council_airllm_result(content, metadata, wall_seconds=wall_seconds)
    assert stopped == [disposable_model]
    assert metadata["ollama_unloaded"] is True
    free_before = metadata.get("free_vram_before_mib")
    free_after = metadata.get("free_vram_after_unload_mib")
    measured_peak = metadata.get("measured_peak_vram_mib")
    assert isinstance(free_before, int | float) and not isinstance(free_before, bool)
    assert isinstance(free_after, int | float) and not isinstance(free_after, bool)
    assert isinstance(measured_peak, int | float) and not isinstance(measured_peak, bool)
    threshold = float(measured_peak) + cyntox_council.MIN_GPU_HEADROOM_MIB
    assert float(free_before) <= threshold
    assert float(free_after) > threshold


def test_resident_backend_plain_and_structured_completion_share_worker() -> None:
    prepared = _prepared_runtime_or_skip()
    schema = {
        "type": "object",
        "properties": {"ready": {"const": True}},
        "required": ["ready"],
        "additionalProperties": False,
    }

    async def exercise() -> None:
        provider = AirLlmProvider(
            ROOT,
            backend="resident",
            max_new_tokens=MAX_LIVE_NEW_TOKENS,
        )
        try:
            await provider.start(timeout=900)
            assert provider.process is not None
            launcher_pid = provider.process.pid
            assert provider.worker_pid is not None
            worker_pid = provider.worker_pid
            session_id = provider._identity["session_id"]
            assert provider._identity["backend_kind"] == "real"
            assert provider._identity["backend_name"] == "transformers-resident"
            assert provider._identity["backend_class"] == "__main__.ResidentModel"
            assert provider._identity["implementation_class"] == (
                "transformers.models.qwen3_5.modeling_qwen3_5.Qwen3_5ForCausalLM"
            )
            assert provider._identity["architecture"] == "Qwen3_5ForConditionalGeneration"
            assert provider._identity["context_limit"] == 8192
            assert provider._identity["model_id"] == MODEL_ID
            assert provider._identity["revision"] == REVISION
            assert provider._identity["gpu_uuid"] == prepared["gpu_uuid"]
            assert provider._identity["startup_peak_vram_mib"] > 0

            identity = await provider.probe()
            assert identity.provider == "qwythos"
            assert identity.runtime == "transformers-resident"
            assert identity.runtime_version == "transformers-5.12.1"
            assert identity.model_id == MODEL_ID
            assert identity.context_limit == 8192

            plain = await provider.complete(
                [
                    {
                        "role": "user",
                        "content": "Reply briefly that the resident Qwythos worker is ready.",
                    }
                ],
                seed=7,
                timeout=900,
            )
            plain_metadata = dict(provider.last_metadata)
            structured = await provider.complete(
                [
                    {
                        "role": "user",
                        "content": 'Return only this JSON object: {"ready": true}',
                    }
                ],
                schema=schema,
                seed=11,
                timeout=900,
            )

            assert provider.process is not None
            assert provider.process.pid == launcher_pid
            assert provider.worker_pid == worker_pid
            assert provider._identity["session_id"] == session_id
            assert plain_metadata["session_id"] == session_id
            assert provider.last_metadata["session_id"] == session_id
            assert plain_metadata["backend_kind"] == "real"
            assert plain_metadata["backend_name"] == "transformers-resident"
            assert provider.last_metadata["backend_kind"] == "real"
            assert provider.last_metadata["backend_name"] == "transformers-resident"
            assert plain_metadata["gpu_uuid"] == prepared["gpu_uuid"]
            assert provider.last_metadata["gpu_uuid"] == prepared["gpu_uuid"]
            assert plain_metadata["peak_vram_mib"] >= plain_metadata["startup_peak_vram_mib"]
            assert (
                provider.last_metadata["peak_vram_mib"]
                >= provider.last_metadata["startup_peak_vram_mib"]
            )

            _assert_no_reasoning_leak(plain.content)
            _assert_no_reasoning_leak(structured.content)
            assert structured.structured == {"ready": True}
            for response in (plain, structured):
                assert response.model.runtime == "transformers-resident"
                assert response.model.runtime_version == "transformers-5.12.1"
                assert response.model.architecture == "Qwen3_5ForConditionalGeneration"
                assert response.model.context_limit == 8192
                assert 0 < response.usage.completion_tokens <= MAX_LIVE_NEW_TOKENS
                assert response.usage.prompt_tokens > 0
        finally:
            await provider.close()
        assert provider.process is None

    asyncio.run(exercise())
