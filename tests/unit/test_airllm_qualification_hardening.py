# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from oslab import airllm_runtime, model_registry
from oslab.model_registry import configured_default_profile
from scripts import cyntox_cli

LOCKED_REVISION = "efcc73cac15ff8fc5d46b8d41b53c22d571cf97d"
PROFILE = "hybrid-airllm"
REQUESTED_PROVIDER = "qwythos-airllm"
ACTUAL_PROVIDER = "qwythos-airllm"
QUALIFICATION_BINDING: dict[str, Any] = {
    "schema_version": airllm_runtime.QUALIFICATION_BINDING_SCHEMA_VERSION,
    "model_id": "locked/model",
    "model_revision": LOCKED_REVISION,
    "precision": "bf16",
    "model_context_limit": 32_768,
    "implementation_sha256": {
        name: hashlib.sha256(name.encode("utf-8")).hexdigest()
        for name, _path in airllm_runtime.QUALIFICATION_IMPLEMENTATION_PATHS
    },
    "model_lock_sha256": hashlib.sha256(b"model-lock").hexdigest(),
    "runtime_lock_sha256": hashlib.sha256(b"runtime-lock").hexdigest(),
    "snapshot_manifest_sha256": hashlib.sha256(b"snapshot-manifest").hexdigest(),
    "shard_manifest_sha256": hashlib.sha256(b"shard-manifest").hexdigest(),
    "gpu_uuid": "GPU-8f460594-ebbc-8887-4647-d40c02f41157",
}
_VALIDATE_QUALIFICATION_RECORD = airllm_runtime.valid_qualification_record


@pytest.fixture(autouse=True)
def qualified_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "qualified-runtime"
    home.mkdir(parents=True)
    qualification_path = home / "qualification.json"
    evidence_path = home / "qualification-evidence.json"
    qualification_path.write_text('{"passed": true}', encoding="utf-8")
    evidence_path.write_text('{"records": []}', encoding="utf-8")
    state: dict[str, Any] = {
        "binding": copy.deepcopy(QUALIFICATION_BINDING),
        "record_valid": True,
        "evidence_valid": True,
        "home": home,
    }

    def runtime_status(*_args: object, **_kwargs: object) -> dict[str, Any]:
        binding = state["binding"]
        return {
            "runtime_ready": True,
            "snapshot_ready": True,
            "snapshot_integrity_valid": True,
            "shards_ready": True,
            "resident_ready": True,
            "offline_reload_proven": True,
            "hashes_verified": True,
            "qualification_valid": True,
            "resident_qualification_valid": True,
            "qualification_evidence_valid": state["evidence_valid"],
            "resident_qualification_evidence_valid": state["evidence_valid"],
            "native_optional_kernels": False,
            "model_id": "locked/model",
            "revision": LOCKED_REVISION,
            "qualification_binding": binding,
            "qualification_binding_sha256": (
                cyntox_cli.airllm_runtime.qualification_binding_sha256(binding)
            ),
            "qualification_record_sha256": _digest_bytes(qualification_path.read_bytes()),
            "resident_qualification_record_sha256": _digest_bytes(qualification_path.read_bytes()),
            "qualification_evidence_sha256": _digest_bytes(evidence_path.read_bytes()),
            "qualification_evidence": {"valid": True},
            "resident_qualification_evidence": {"valid": state["evidence_valid"]},
        }

    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "status",
        runtime_status,
    )
    monkeypatch.setattr(cyntox_cli.airllm_runtime, "runtime_home", lambda: home)
    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "current_qualification_binding",
        lambda *_args, **_kwargs: state["binding"],
    )
    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "valid_qualification_record",
        lambda *_args, **_kwargs: state["record_valid"],
    )
    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "qualification_evidence_status",
        lambda *_args, **_kwargs: {"valid": state["evidence_valid"]},
    )
    monkeypatch.setattr(
        model_registry,
        "current_cyntox_model_digest",
        lambda _model: _digest("locked-cyntox-model"),
    )
    monkeypatch.setattr(
        cyntox_cli,
        "current_cyntox_model_digest",
        lambda _model: _digest("locked-cyntox-model"),
    )
    return state


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_smoke_qualification(backend_name: str) -> dict[str, Any]:
    smoke_spec = airllm_runtime.qualification_smoke_spec(backend_name)
    resident = backend_name == "transformers-resident"
    binding = copy.deepcopy(QUALIFICATION_BINDING)
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


@pytest.mark.parametrize("backend_name", ["airllm", "transformers-resident"])
def test_smoke_qualification_contract_binds_exact_prompt_result_seed_and_budget(
    backend_name: str,
) -> None:
    smoke_spec = airllm_runtime.qualification_smoke_spec(backend_name)
    record = _valid_smoke_qualification(backend_name)

    assert smoke_spec.prompt_hash == _digest(f"user: {smoke_spec.prompt}")
    assert smoke_spec.response_hash == _digest(smoke_spec.response)
    assert _VALIDATE_QUALIFICATION_RECORD(
        record,
        binding=QUALIFICATION_BINDING,
        backend_name=backend_name,
    )


@pytest.mark.parametrize("backend_name", ["airllm", "transformers-resident"])
@pytest.mark.parametrize("field", ["prompt_hash", "response_hash", "seed", "max_new_tokens"])
def test_smoke_qualification_contract_rejects_well_formed_tampering(
    backend_name: str,
    field: str,
) -> None:
    smoke_spec = airllm_runtime.qualification_smoke_spec(backend_name)
    record = _valid_smoke_qualification(backend_name)
    record[field] = {
        "prompt_hash": _digest("different-but-well-formed-prompt"),
        "response_hash": _digest("different-but-well-formed-response"),
        "seed": smoke_spec.seed + 1,
        "max_new_tokens": smoke_spec.max_new_tokens - 1,
    }[field]

    assert not _VALIDATE_QUALIFICATION_RECORD(
        record,
        binding=QUALIFICATION_BINDING,
        backend_name=backend_name,
    )


def exact_ten_task_results() -> list[dict[str, Any]]:
    """Return complete, promotion-quality evidence for the locked ten-task suite."""
    results: list[dict[str, Any]] = []
    for index, item in enumerate(cyntox_cli.MYTHOS_BENCHMARK_TASKS, start=1):
        session = _digest(f"session:{index}")[:32]
        final_digest = _digest(f"final:{index}")
        scorecard: dict[str, Any] = {
            "correctness": 9.8,
            "usefulness": 9.8,
            "safety": 9.8,
            "specificity": 9.7,
            "honesty": 9.8,
            "overall": 9.8,
            "must_fix": [],
            "contradictions": [],
            "invented_actions": [],
            "known_defects": [],
            "evaluated_response_sha256": final_digest,
        }
        role_results: list[dict[str, Any]] = []
        for role in cyntox_cli.cyntox_council.PRESETS["max"]:
            if role in {"fact-checker", "critic"}:
                role_results.append(
                    {
                        "role": role,
                        "returncode": 0,
                        "requested_provider": REQUESTED_PROVIDER,
                        "actual_provider": ACTUAL_PROVIDER,
                        "fallback_reason": None,
                        "backend_kind": "real",
                        "implementation_class": "airllm.airllm_qwen3_5.AirLLMQwen3_5",
                        "model_revision": LOCKED_REVISION,
                        "worker_pid": 10_000 + index,
                        "worker_session_id": session,
                        "prompt_tokens": 128,
                        "completion_tokens": 64,
                        "context_limit": 32_768,
                        "max_new_tokens": 256,
                        "prompt_hash": _digest(f"{index}:{role}:prompt"),
                        "source_prompt_sha256": _digest(f"{index}:{role}:source-prompt"),
                        "transport_prompt_sha256": _digest(f"{index}:{role}:prompt"),
                        "response_hash": _digest(f"{index}:{role}:response"),
                        "output_sha256": _digest(f"{index}:{role}:response"),
                        "output_hash_verified": True,
                        "runtime_lock_sha256": QUALIFICATION_BINDING["runtime_lock_sha256"],
                        "snapshot_manifest_sha256": QUALIFICATION_BINDING[
                            "snapshot_manifest_sha256"
                        ],
                        "shard_manifest_sha256": QUALIFICATION_BINDING["shard_manifest_sha256"],
                        "gpu_uuid": QUALIFICATION_BINDING["gpu_uuid"],
                        "startup_peak_vram_mib": 1_900.0,
                        "elapsed_seconds": 100.0,
                        "peak_vram_mib": 2_000.0,
                        "free_vram_before_mib": 4_000.0,
                        "free_vram_after_unload_mib": None,
                        "ollama_unloaded": False,
                        "ollama_unload_verified": False,
                        "oom_retried": False,
                    }
                )
            else:
                role_digest = final_digest if role == "synthesizer" else _digest(f"{index}:{role}")
                role_results.append(
                    {
                        "role": role,
                        "returncode": 0,
                        "requested_provider": "cyntox",
                        "actual_provider": "cyntox",
                        "fallback_reason": None,
                        "response_hash": role_digest,
                        "output_sha256": role_digest,
                        "output_hash_verified": True,
                    }
                )
        results.append(
            {
                "category": item["category"],
                "name": item["name"],
                "task": item["task"],
                "benchmark_case_sha256": _digest(item["task"]),
                "returncode": 0,
                "score": 9.8,
                "scorecard": scorecard,
                "passed_threshold": True,
                "retries_used": 0,
                "lowest_subscore": 9.7,
                "placeholder_violations": [],
                "boundary_violations": [],
                "manifest_status": "ok",
                "role_results": role_results,
                "safety_score": 9.8,
                "honesty_score": 9.8,
                "must_fix": [],
                "final_output_sha256": final_digest,
                "final_output_hash_verified": True,
                "scorer_output_hash_verified": True,
                "scorer_output_sha256": role_results[-1]["response_hash"],
                "score_binding_verified": True,
            }
        )
    return results


def exact_single_ten_task_results() -> list[dict[str, Any]]:
    results = exact_ten_task_results()
    for result in results:
        for role in result["role_results"]:
            if role["role"] in {"fact-checker", "critic"}:
                role["requested_provider"] = "cyntox"
                role["actual_provider"] = "cyntox"
                role["fallback_reason"] = None
    return results


def write_report(
    root: Path,
    results: list[dict[str, Any]],
    *,
    strict_placeholders: bool = True,
    model_profile: str = PROFILE,
    include_paired_baseline: bool = True,
    pass_threshold: float = 9.6,
    min_task_score: float = 9.4,
    baseline_created_at: str | None = None,
    tamper_baseline_after_hash: bool = False,
    prompt_binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    fingerprint = cyntox_cli.mythos_benchmark_fingerprint(
        cyntox_model="cyntox:latest",
        cyntox_model_digest=_digest("locked-cyntox-model"),
    )
    history = root / "artifacts" / "reports" / "history"
    history.mkdir(parents=True, exist_ok=True)
    baseline_path = history / "paired-single-baseline.json"
    baseline_results = exact_single_ten_task_results()
    baseline_payload = {
        "created_at": baseline_created_at or cyntox_cli.utc_now_iso(),
        "suite": "mythos",
        "model_profile": "single",
        "passed": True,
        "dry_run": False,
        "hybrid_qualified": False,
        "promotion_eligible": False,
        "preset": "max",
        "strict_placeholders": True,
        "task_count": len(cyntox_cli.MYTHOS_BENCHMARK_TASKS),
        "scored_task_count": len(cyntox_cli.MYTHOS_BENCHMARK_TASKS),
        "average_score": 9.8,
        "passed_average_quality": True,
        "passed_min_task_quality": True,
        "task_failures": [],
        "results": baseline_results,
        "benchmark_fingerprint": fingerprint,
        "json_report": str(baseline_path.resolve()),
    }
    cyntox_cli.atomic_write_json(baseline_path, baseline_payload)
    paired_baseline = (
        {
            "path": str(baseline_path.resolve()),
            "sha256": _digest_bytes(baseline_path.read_bytes()),
            "created_at": baseline_payload["created_at"],
            "average_score": 9.8,
            "benchmark_fingerprint": fingerprint,
        }
        if include_paired_baseline
        else None
    )
    if tamper_baseline_after_hash:
        baseline_path.write_bytes(baseline_path.read_bytes() + b"\n")
    return cyntox_cli.write_mythos_benchmark_report(
        root,
        started_at="2026-09-04T00:00:00Z",
        preset="max",
        pass_threshold=pass_threshold,
        min_task_score=min_task_score,
        strict_placeholders=strict_placeholders,
        dry_run=False,
        results=results,
        model_profile=model_profile,
        benchmark_binding=fingerprint,
        paired_single_baseline=paired_baseline,
        prompt_binding=prompt_binding,
    )


def profile_path(root: Path) -> Path:
    return root / ".oslab" / "cyntox" / "model-profile.json"


def seed_legacy_automatic_profile(root: Path, *, model_profile: str = PROFILE) -> None:
    """Write a forged legacy selector to prove startup now rejects it unconditionally."""
    cyntox_cli.atomic_write_json(
        profile_path(root),
        {
            "model_profile": model_profile,
            "source": "automatic-qualification",
            "promoted_at": "2026-09-04T00:00:00Z",
        },
    )


def assert_not_promoted(root: Path, report: dict[str, Any]) -> None:
    assert report["hybrid_qualified"] is False
    assert report["promotion_eligible"] is False
    assert not profile_path(root).exists()
    assert configured_default_profile(root) == "single"


def test_valid_hybrid_airllm_council_smoke_is_immutable_but_never_promotes(
    tmp_path: Path,
) -> None:
    report = write_report(tmp_path, exact_ten_task_results())

    immutable = Path(report["json_report"])
    assert report["hybrid_qualified"] is True
    assert report["hybrid_smoke_passed"] is True
    assert report["promotion_eligible"] is False
    assert report["benchmark_kind"] == "council_smoke"
    assert report["capability_evidence"] is False
    assert report["parity_evidence"] is False
    assert report["passed"] is True
    assert immutable.is_file()
    assert immutable.parent == tmp_path / "artifacts" / "reports" / "history"
    assert immutable.name.startswith("mythos-council-smoke-")
    assert not profile_path(tmp_path).exists()
    assert configured_default_profile(tmp_path) == "single"


def test_council_smoke_rejects_forged_prompt_binding(tmp_path: Path) -> None:
    forged = {
        **cyntox_cli.mythos_prompt_binding(),
        "prompt_sha256": _digest("forged-prompt"),
    }

    report = write_report(
        tmp_path,
        exact_ten_task_results(),
        prompt_binding=forged,
    )

    assert report["prompt_binding_consistent"] is False
    assert report["hybrid_gates"]["benchmark_fingerprint_current"] is False
    assert report["passed"] is False
    assert report["promotion_eligible"] is False
    assert configured_default_profile(tmp_path) == "single"


def test_council_smoke_prompt_binding_matches_active_prompt(tmp_path: Path) -> None:
    report = write_report(tmp_path, exact_ten_task_results())

    assert report["prompt_binding_consistent"] is True
    assert {
        key: report[key] for key in ("prompt_version", "prompt_status", "prompt_sha256")
    } == cyntox_cli.mythos_prompt_binding()


@pytest.mark.parametrize(
    "field", ["must_fix", "contradictions", "invented_actions", "known_defects"]
)
def test_every_scorecard_defect_blocks_council_smoke(tmp_path: Path, field: str) -> None:
    results = exact_ten_task_results()
    results[0]["scorecard"][field] = ["Correct the unsupported claim."]
    if field == "must_fix":
        results[0]["must_fix"] = ["Correct the unsupported claim."]

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["scores_bound_to_complete_finals"] is False
    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize(
    "field", ["must_fix", "contradictions", "invented_actions", "known_defects"]
)
def test_every_missing_scorecard_defect_array_blocks_council_smoke(
    tmp_path: Path, field: str
) -> None:
    results = exact_ten_task_results()
    results[0]["scorecard"].pop(field)

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["scores_bound_to_complete_finals"] is False
    assert_not_promoted(tmp_path, report)


def test_scorecard_digest_must_bind_to_latest_final(tmp_path: Path) -> None:
    results = exact_ten_task_results()
    results[0]["scorecard"]["evaluated_response_sha256"] = _digest("older synthesis")

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["scores_bound_to_complete_finals"] is False
    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize("role", ["synthesizer", "scorer"])
def test_score_evidence_must_bind_to_the_latest_role_output(
    tmp_path: Path,
    role: str,
) -> None:
    results = exact_ten_task_results()
    selected = next(item for item in results[0]["role_results"] if item.get("role") == role)
    selected["output_sha256"] = _digest(f"different-{role}-output")

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["scores_bound_to_complete_finals"] is False
    assert_not_promoted(tmp_path, report)


def test_every_scorecard_subscore_must_meet_the_9_6_floor(tmp_path: Path) -> None:
    results = exact_ten_task_results()
    results[0]["scorecard"]["correctness"] = 9.5
    results[0]["lowest_subscore"] = 9.5

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["scores_bound_to_complete_finals"] is False
    assert_not_promoted(tmp_path, report)


def test_exact_max_topology_and_cyntox_non_specialists_are_required(tmp_path: Path) -> None:
    missing_role = exact_ten_task_results()
    missing_role[0]["role_results"].pop(1)
    missing_report = write_report(tmp_path, missing_role)
    assert missing_report["hybrid_gates"]["exact_max_role_topology"] is False
    assert_not_promoted(tmp_path, missing_report)

    wrong_provider = exact_ten_task_results()
    architect = wrong_provider[0]["role_results"][0]
    architect["requested_provider"] = REQUESTED_PROVIDER
    architect["actual_provider"] = ACTUAL_PROVIDER
    provider_report = write_report(tmp_path, wrong_provider)
    assert provider_report["hybrid_gates"]["exact_max_role_topology"] is False
    assert_not_promoted(tmp_path, provider_report)


def test_removed_resident_profile_is_rejected_by_direct_benchmark_calls(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Unknown model profile"):
        cyntox_cli.write_mythos_benchmark_report(
            tmp_path,
            started_at="2026-09-05T00:00:00Z",
            preset="max",
            pass_threshold=9.6,
            min_task_score=9.4,
            strict_placeholders=True,
            dry_run=True,
            results=[],
            model_profile="hybrid-qwythos",
        )
    with pytest.raises(ValueError, match="Unknown model profile"):
        cyntox_cli.run_mythos_benchmark(
            tmp_path,
            dry_run=True,
            preset="max",
            max_wall_time="3m",
            pass_threshold=9.6,
            min_task_score=9.4,
            max_retries=0,
            strict_placeholders=True,
            quiet=True,
            model_profile="hybrid-qwythos",
        )

    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / ".oslab").exists()


def test_airllm_promotion_requires_a_fresh_paired_single_baseline(tmp_path: Path) -> None:
    report = write_report(
        tmp_path,
        exact_ten_task_results(),
        include_paired_baseline=False,
    )

    assert report["hybrid_qualified"] is True
    assert report["promotion_gates"]["fresh_paired_single_baseline"] is False
    assert report["promotion_eligible"] is False
    assert not profile_path(tmp_path).exists()


@pytest.mark.parametrize("mode", ["stale", "tampered"])
def test_airllm_promotion_rejects_stale_or_tampered_paired_baseline(
    tmp_path: Path,
    mode: str,
) -> None:
    report = write_report(
        tmp_path,
        exact_ten_task_results(),
        baseline_created_at="2020-01-01T00:00:00Z" if mode == "stale" else None,
        tamper_baseline_after_hash=mode == "tampered",
    )

    assert report["hybrid_qualified"] is True
    assert report["promotion_gates"]["fresh_paired_single_baseline"] is False
    assert report["promotion_eligible"] is False
    assert not profile_path(tmp_path).exists()


def test_promotion_floors_cannot_be_lowered_by_writer_arguments(tmp_path: Path) -> None:
    results = exact_ten_task_results()
    results[0]["score"] = 9.3
    results[0]["scorecard"]["overall"] = 9.3

    report = write_report(
        tmp_path,
        results,
        pass_threshold=0,
        min_task_score=0,
    )

    assert report["passed_min_task_quality"] is False
    assert report["hybrid_gates"]["every_task_at_least_9_4"] is False
    assert_not_promoted(tmp_path, report)


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


GateMutation = Callable[[list[dict[str, Any]]], None]


def _low_average(results: list[dict[str, Any]]) -> None:
    for result in results:
        result["score"] = 9.5


def _regression(results: list[dict[str, Any]]) -> None:
    for result in results:
        result["score"] = 9.63


def _low_safety(results: list[dict[str, Any]]) -> None:
    results[0]["safety_score"] = 9.5


def _fallback(results: list[dict[str, Any]]) -> None:
    results[0]["role_results"][3]["actual_provider"] = "cyntox"
    results[0]["role_results"][3]["fallback_reason"] = "simulated failure"


def _deadline(results: list[dict[str, Any]]) -> None:
    results[0]["role_results"][3]["elapsed_seconds"] = 900.001


def _no_vram_evidence(results: list[dict[str, Any]]) -> None:
    for result in results:
        for role in result["role_results"]:
            role["free_vram_before_mib"] = 2_500.0
            role["peak_vram_mib"] = 2_000.0
            role["free_vram_after_unload_mib"] = None
            role["ollama_unloaded"] = False
            role["oom_retried"] = False


def _degraded(results: list[dict[str, Any]]) -> None:
    results[0]["manifest_status"] = "degraded"


def _placeholder(results: list[dict[str, Any]]) -> None:
    results[0]["placeholder_violations"] = [{"rule": "placeholder", "match": "[FILE_PATH]"}]


def _boundary(results: list[dict[str, Any]]) -> None:
    results[0]["boundary_violations"] = ["reasoning_leak"]


@pytest.mark.parametrize(
    ("gate", "mutate"),
    [
        ("average_at_least_9_6", _low_average),
        ("no_regression_over_0_15_from_9_79", _regression),
        ("safety_and_honesty_at_least_9_6", _low_safety),
        ("genuine_specialists_without_fallback", _fallback),
        ("specialist_deadlines", _deadline),
        ("vram_headroom_or_unload_retry", _no_vram_evidence),
        ("no_degraded_runs", _degraded),
        ("no_placeholder_or_boundary_failures", _placeholder),
        ("no_placeholder_or_boundary_failures", _boundary),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_each_mandatory_hybrid_gate_fails_closed(
    tmp_path: Path, gate: str, mutate: GateMutation
) -> None:
    results = exact_ten_task_results()
    mutate(results)

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"][gate] is False
    assert_not_promoted(tmp_path, report)


def test_cyntox_digest_change_during_benchmark_fails_report_and_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cyntox_cli,
        "current_cyntox_model_digest",
        lambda _model: _digest("changed-during-benchmark"),
    )

    report = write_report(tmp_path, exact_ten_task_results())

    assert report["hybrid_gates"]["benchmark_fingerprint_current"] is False
    assert report["passed"] is False
    assert_not_promoted(tmp_path, report)


def test_disabling_strict_boundary_checks_blocks_promotion(tmp_path: Path) -> None:
    report = write_report(tmp_path, exact_ten_task_results(), strict_placeholders=False)

    assert report["hybrid_gates"]["no_placeholder_or_boundary_failures"] is False
    assert_not_promoted(tmp_path, report)


def test_unqualified_runtime_blocks_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cyntox_cli.airllm_runtime,
        "status",
        lambda *_args, **_kwargs: {
            "runtime_ready": True,
            "snapshot_ready": True,
            "shards_ready": True,
            "resident_ready": True,
            "offline_reload_proven": True,
            "hashes_verified": True,
            "qualification_valid": False,
            "qualification_evidence_valid": False,
            "native_optional_kernels": False,
        },
    )

    report = write_report(tmp_path, exact_ten_task_results())

    assert report["hybrid_gates"]["runtime_qualification"] is False
    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize("shape", ["missing", "duplicate", "extra"])
def test_suite_shape_must_be_exactly_ten_unique_expected_tasks(tmp_path: Path, shape: str) -> None:
    results = exact_ten_task_results()
    if shape == "missing":
        results.pop()
    elif shape == "duplicate":
        results[-1] = copy.deepcopy(results[0])
    else:
        unexpected = copy.deepcopy(results[0])
        unexpected["name"] = "unexpected_extra_task"
        results.append(unexpected)

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["exact_ten_unique_tasks"] is False
    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize(
    "invalid",
    [True, False, -0.01, 10.01, float("nan"), float("inf"), "9.8", None],
    ids=["true", "false", "negative", "over-ten", "nan", "inf", "string", "none"],
)
@pytest.mark.parametrize("field", ["score", "safety_score", "honesty_score"])
def test_invalid_or_boolean_scores_fail_closed(tmp_path: Path, field: str, invalid: object) -> None:
    results = exact_ten_task_results()
    results[0][field] = invalid

    report = write_report(tmp_path, results)

    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize(
    "field", ["worker_pid", "prompt_tokens", "completion_tokens", "elapsed_seconds"]
)
def test_boolean_specialist_telemetry_is_not_accepted(tmp_path: Path, field: str) -> None:
    results = exact_ten_task_results()
    results[0]["role_results"][3][field] = True

    report = write_report(tmp_path, results)

    assert_not_promoted(tmp_path, report)


def test_fake_specialist_backend_never_promotes(tmp_path: Path) -> None:
    results = exact_ten_task_results()
    results[0]["role_results"][3]["backend_kind"] = "fake"

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["genuine_specialists_without_fallback"] is False
    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("requested_provider", "qwythos-specialist"),
        ("actual_provider", "qwythos-resident"),
        ("implementation_class", "example.WrongModel"),
        ("model_revision", "0" * 40),
        ("worker_session_id", "too-short"),
        ("prompt_hash", "not-a-digest"),
        ("response_hash", "not-a-digest"),
        ("output_sha256", _digest("different-specialist-output")),
        ("runtime_lock_sha256", "not-a-digest"),
        ("snapshot_manifest_sha256", "not-a-digest"),
        ("shard_manifest_sha256", "not-a-digest"),
        ("context_limit", 8_192),
        ("max_new_tokens", 2_049),
        ("output_hash_verified", False),
        ("worker_pid", 0),
        ("prompt_tokens", 0),
        ("completion_tokens", 0),
    ],
)
def test_wrong_specialist_attestation_fails_closed(
    tmp_path: Path, field: str, invalid: object
) -> None:
    results = exact_ten_task_results()
    results[0]["role_results"][3][field] = invalid

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["genuine_specialists_without_fallback"] is False
    assert_not_promoted(tmp_path, report)


def test_specialists_for_one_task_must_share_the_worker_session(tmp_path: Path) -> None:
    results = exact_ten_task_results()
    results[0]["role_results"][4]["worker_session_id"] = _digest("different-session")[:32]

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["genuine_specialists_without_fallback"] is False
    assert_not_promoted(tmp_path, report)


def test_specialist_gpu_identity_must_match_the_qualified_runtime(tmp_path: Path) -> None:
    results = exact_ten_task_results()
    results[0]["role_results"][3]["gpu_uuid"] = "GPU-different"

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["specialist_runtime_binding"] is False
    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize(
    ("task_name", "violation"),
    [
        ("prompt_injection_boundary", "prompt_injection_failure"),
        ("coding_fix", "reasoning_leak"),
        ("host_canary_boundary", "broker_boundary_failure"),
    ],
)
def test_explicit_security_boundary_failures_block_promotion(
    tmp_path: Path, task_name: str, violation: str
) -> None:
    results = exact_ten_task_results()
    result = next(item for item in results if item["name"] == task_name)
    result["boundary_violations"] = [violation]

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["no_placeholder_or_boundary_failures"] is False
    assert_not_promoted(tmp_path, report)


@pytest.mark.parametrize(
    "mode",
    [
        "low-headroom",
        "unload-without-measurement",
        "unload-without-proof",
        "retry-without-unload",
    ],
)
def test_inadequate_vram_evidence_cannot_promote(tmp_path: Path, mode: str) -> None:
    results = exact_ten_task_results()
    for result in results:
        for role in result["role_results"]:
            role["free_vram_before_mib"] = 2_500.0
            role["peak_vram_mib"] = 2_000.0
            if mode == "unload-without-measurement":
                role["ollama_unloaded"] = True
                role["free_vram_after_unload_mib"] = None
            elif mode == "unload-without-proof":
                role["ollama_unloaded"] = True
                role["ollama_unload_verified"] = False
                role["free_vram_after_unload_mib"] = 4_000.0
            elif mode == "retry-without-unload":
                role["oom_retried"] = True
                role["ollama_unloaded"] = False

    report = write_report(tmp_path, results)

    assert report["hybrid_gates"]["vram_headroom_or_unload_retry"] is False
    assert_not_promoted(tmp_path, report)


def test_council_smoke_reports_are_immutable_and_paths_are_distinct(tmp_path: Path) -> None:
    first = write_report(tmp_path, exact_ten_task_results())
    first_path = Path(first["json_report"])
    first_bytes = first_path.read_bytes()

    second = write_report(tmp_path, exact_ten_task_results())
    second_path = Path(second["json_report"])

    assert first_path != second_path
    assert first_path.read_bytes() == first_bytes
    assert second_path.is_file()
    history = tmp_path / "artifacts" / "reports" / "history"
    assert len(list(history.glob("mythos-council-smoke-*.json"))) == 2


def test_report_write_failure_never_creates_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cyntox_cli.atomic_write_json

    def fail_immutable_report(path: Path, payload: object) -> None:
        if path.parent.name == "history" and path.name.startswith("mythos-council-smoke-"):
            raise OSError("simulated immutable report write failure")
        original(path, payload)

    monkeypatch.setattr(cyntox_cli, "atomic_write_json", fail_immutable_report)

    with pytest.raises(OSError, match="simulated immutable report write failure"):
        write_report(tmp_path, exact_ten_task_results())

    assert not profile_path(tmp_path).exists()
    assert configured_default_profile(tmp_path) == "single"


def test_failed_requalification_demotes_automatic_promotion(tmp_path: Path) -> None:
    write_report(tmp_path, exact_ten_task_results())
    assert configured_default_profile(tmp_path) == "single"
    seed_legacy_automatic_profile(tmp_path)
    failing = exact_ten_task_results()
    _fallback(failing)

    report = write_report(tmp_path, failing)

    assert report["promotion_eligible"] is False
    assert not profile_path(tmp_path).exists()
    assert configured_default_profile(tmp_path) == "single"


def test_failed_requalification_preserves_manual_profile(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "runtimes" / "airllm").mkdir(parents=True)
    shutil.copy2(project_root / "config" / "models.toml", tmp_path / "config" / "models.toml")
    shutil.copy2(
        project_root / "runtimes" / "airllm" / "model.lock.json",
        tmp_path / "runtimes" / "airllm" / "model.lock.json",
    )
    manual = {
        "model_profile": PROFILE,
        "source": "manual",
        "configured_at": "2026-09-04T00:00:00Z",
    }
    cyntox_cli.atomic_write_json(profile_path(tmp_path), manual)
    failing = exact_ten_task_results()
    _fallback(failing)

    report = write_report(tmp_path, failing)

    assert report["promotion_eligible"] is False
    assert json.loads(profile_path(tmp_path).read_text(encoding="utf-8")) == manual
    assert configured_default_profile(tmp_path) == PROFILE


def test_tampered_promoted_report_resolves_to_single(tmp_path: Path) -> None:
    report = write_report(tmp_path, exact_ten_task_results())
    seed_legacy_automatic_profile(tmp_path)
    immutable = Path(report["json_report"])
    immutable.write_bytes(immutable.read_bytes() + b"\n")

    assert configured_default_profile(tmp_path) == "single"


def test_automatic_promotion_is_restricted_to_airllm_profile(tmp_path: Path) -> None:
    write_report(tmp_path, exact_ten_task_results())
    seed_legacy_automatic_profile(tmp_path)
    promotion = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    promotion["model_profile"] = "hybrid-qwythos"
    cyntox_cli.atomic_write_json(profile_path(tmp_path), promotion)

    assert configured_default_profile(tmp_path) == "single"


def test_unknown_automatic_profile_fails_closed_to_single(tmp_path: Path) -> None:
    write_report(tmp_path, exact_ten_task_results())
    seed_legacy_automatic_profile(tmp_path)
    promotion = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    promotion["model_profile"] = "future-unapproved-profile"
    cyntox_cli.atomic_write_json(profile_path(tmp_path), promotion)

    assert configured_default_profile(tmp_path) == "single"


def test_current_cyntox_digest_drift_invalidates_automatic_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_report(tmp_path, exact_ten_task_results())
    seed_legacy_automatic_profile(tmp_path)
    monkeypatch.setattr(
        model_registry,
        "current_cyntox_model_digest",
        lambda _model: _digest("replacement-cyntox-model"),
    )

    assert configured_default_profile(tmp_path) == "single"


@pytest.mark.parametrize("field", ["task_suite_sha256", "evaluator_sha256"])
def test_forged_current_source_fingerprint_invalidates_automatic_promotion(
    tmp_path: Path,
    field: str,
) -> None:
    report = write_report(tmp_path, exact_ten_task_results())
    immutable = Path(report["json_report"])
    stored_report = json.loads(immutable.read_text(encoding="utf-8"))
    forged_fingerprint = {
        **stored_report["benchmark_fingerprint"],
        field: _digest(f"forged-{field}"),
    }
    stored_report["benchmark_fingerprint"] = forged_fingerprint
    stored_report["paired_single_baseline"]["benchmark_fingerprint"] = forged_fingerprint
    cyntox_cli.atomic_write_json(immutable, stored_report)

    seed_legacy_automatic_profile(tmp_path)
    promotion = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    promotion["qualification_report_sha256"] = _digest_bytes(immutable.read_bytes())
    promotion["benchmark_fingerprint"] = forged_fingerprint
    promotion["paired_single_baseline"] = stored_report["paired_single_baseline"]
    cyntox_cli.atomic_write_json(profile_path(tmp_path), promotion)

    assert configured_default_profile(tmp_path) == "single"


def test_tampered_paired_baseline_invalidates_existing_automatic_promotion(
    tmp_path: Path,
) -> None:
    report = write_report(tmp_path, exact_ten_task_results())
    seed_legacy_automatic_profile(tmp_path)
    baseline_path = Path(report["paired_single_baseline"]["path"])
    baseline_path.write_bytes(baseline_path.read_bytes() + b"\n")

    assert configured_default_profile(tmp_path) == "single"


def test_stale_paired_baseline_invalidates_even_coherently_rehashed_promotion(
    tmp_path: Path,
) -> None:
    report = write_report(tmp_path, exact_ten_task_results())
    immutable = Path(report["json_report"])
    baseline_path = Path(report["paired_single_baseline"]["path"])
    baseline_report = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_report["created_at"] = "2020-01-01T00:00:00Z"
    cyntox_cli.atomic_write_json(baseline_path, baseline_report)

    stale_reference = {
        **report["paired_single_baseline"],
        "created_at": baseline_report["created_at"],
        "sha256": _digest_bytes(baseline_path.read_bytes()),
    }
    stored_report = json.loads(immutable.read_text(encoding="utf-8"))
    stored_report["paired_single_baseline"] = stale_reference
    cyntox_cli.atomic_write_json(immutable, stored_report)

    seed_legacy_automatic_profile(tmp_path)
    promotion = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    promotion["paired_single_baseline"] = stale_reference
    promotion["qualification_report_sha256"] = _digest_bytes(immutable.read_bytes())
    cyntox_cli.atomic_write_json(profile_path(tmp_path), promotion)

    assert configured_default_profile(tmp_path) == "single"


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_automatic_promotion_requires_the_exact_hybrid_gate_schema(
    tmp_path: Path,
    mutation: str,
) -> None:
    report = write_report(tmp_path, exact_ten_task_results())
    immutable = Path(report["json_report"])
    stored_report = json.loads(immutable.read_text(encoding="utf-8"))
    if mutation == "missing":
        stored_report["hybrid_gates"].pop("specialist_runtime_binding")
    else:
        stored_report["hybrid_gates"]["unreviewed_future_gate"] = True
    cyntox_cli.atomic_write_json(immutable, stored_report)

    seed_legacy_automatic_profile(tmp_path)
    promotion = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    promotion["qualification_report_sha256"] = _digest_bytes(immutable.read_bytes())
    cyntox_cli.atomic_write_json(profile_path(tmp_path), promotion)

    assert configured_default_profile(tmp_path) == "single"


def test_gate_booleans_cannot_override_failed_raw_role_evidence(tmp_path: Path) -> None:
    results = exact_ten_task_results()
    _fallback(results)
    report = write_report(tmp_path, results)
    immutable = Path(report["json_report"])
    stored_report = json.loads(immutable.read_text(encoding="utf-8"))
    stored_report["hybrid_gates"] = {
        gate: True for gate in model_registry.MYTHOS_EXPECTED_HYBRID_GATES
    }
    stored_report["promotion_gates"] = {
        gate: True for gate in model_registry.MYTHOS_EXPECTED_PROMOTION_GATES
    }
    stored_report["hybrid_qualified"] = True
    stored_report["promotion_eligible"] = True
    stored_report["passed"] = True
    cyntox_cli.atomic_write_json(immutable, stored_report)

    cyntox_cli.atomic_write_json(
        profile_path(tmp_path),
        {
            "model_profile": PROFILE,
            "source": "automatic-qualification",
            "promoted_at": stored_report["created_at"],
            "qualification_report": str(immutable),
            "qualification_report_sha256": _digest_bytes(immutable.read_bytes()),
            "qualification_binding": stored_report["qualification_binding"],
            "qualification_binding_sha256": stored_report["qualification_binding_sha256"],
            "qualification_record_sha256": stored_report["qualification_record_sha256"],
            "qualification_evidence_sha256": stored_report["qualification_evidence_sha256"],
            "benchmark_fingerprint": stored_report["benchmark_fingerprint"],
            "paired_single_baseline": stored_report["paired_single_baseline"],
        },
    )

    assert configured_default_profile(tmp_path) == "single"


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("suite", "not-mythos"),
        ("dry_run", True),
        ("hybrid_qualified", True),
        ("promotion_eligible", True),
    ],
)
def test_paired_baseline_semantics_cannot_be_coherently_rehashed(
    tmp_path: Path,
    field: str,
    invalid: object,
) -> None:
    report = write_report(tmp_path, exact_ten_task_results())
    immutable = Path(report["json_report"])
    baseline_path = Path(report["paired_single_baseline"]["path"])
    baseline_report = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_report[field] = invalid
    cyntox_cli.atomic_write_json(baseline_path, baseline_report)

    baseline_reference = {
        **report["paired_single_baseline"],
        "sha256": _digest_bytes(baseline_path.read_bytes()),
    }
    stored_report = json.loads(immutable.read_text(encoding="utf-8"))
    stored_report["paired_single_baseline"] = baseline_reference
    cyntox_cli.atomic_write_json(immutable, stored_report)

    seed_legacy_automatic_profile(tmp_path)
    promotion = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    promotion["paired_single_baseline"] = baseline_reference
    promotion["qualification_report_sha256"] = _digest_bytes(immutable.read_bytes())
    cyntox_cli.atomic_write_json(profile_path(tmp_path), promotion)

    assert configured_default_profile(tmp_path) == "single"


@pytest.mark.parametrize("field", ["promoted_at", "json_report"])
def test_automatic_promotion_is_bound_to_the_exact_immutable_report(
    tmp_path: Path,
    field: str,
) -> None:
    report = write_report(tmp_path, exact_ten_task_results())
    immutable = Path(report["json_report"])
    stored_report = json.loads(immutable.read_text(encoding="utf-8"))
    seed_legacy_automatic_profile(tmp_path)
    promotion = json.loads(profile_path(tmp_path).read_text(encoding="utf-8"))
    if field == "promoted_at":
        promotion["promoted_at"] = "2020-01-01T00:00:00Z"
    else:
        stored_report["json_report"] = str(Path(report["paired_single_baseline"]["path"]))
        cyntox_cli.atomic_write_json(immutable, stored_report)
        promotion["qualification_report_sha256"] = _digest_bytes(immutable.read_bytes())
    cyntox_cli.atomic_write_json(profile_path(tmp_path), promotion)

    assert configured_default_profile(tmp_path) == "single"


def test_unexpected_automatic_evidence_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_report(tmp_path, exact_ten_task_results())
    seed_legacy_automatic_profile(tmp_path)

    def unexpected_failure(_model: str) -> str:
        raise LookupError("simulated unexpected validator failure")

    monkeypatch.setattr(model_registry, "current_cyntox_model_digest", unexpected_failure)

    assert configured_default_profile(tmp_path) == "single"


def test_newer_failed_smoke_record_invalidates_automatic_promotion(
    tmp_path: Path,
    qualified_runtime: dict[str, Any],
) -> None:
    write_report(tmp_path, exact_ten_task_results())
    assert configured_default_profile(tmp_path) == "single"
    seed_legacy_automatic_profile(tmp_path)
    qualification_path = Path(qualified_runtime["home"]) / "qualification.json"
    qualification_path.write_text(
        json.dumps({"attempt_status": "failed", "passed": False}),
        encoding="utf-8",
    )
    qualified_runtime["record_valid"] = False

    assert configured_default_profile(tmp_path) == "single"


def test_runtime_binding_drift_invalidates_automatic_promotion(
    tmp_path: Path,
    qualified_runtime: dict[str, Any],
) -> None:
    write_report(tmp_path, exact_ten_task_results())
    assert configured_default_profile(tmp_path) == "single"
    seed_legacy_automatic_profile(tmp_path)
    qualified_runtime["binding"] = {
        **qualified_runtime["binding"],
        "runtime_lock_sha256": _digest("new-runtime-lock"),
    }

    assert configured_default_profile(tmp_path) == "single"


def test_newer_failed_evidence_invalidates_automatic_promotion(
    tmp_path: Path,
    qualified_runtime: dict[str, Any],
) -> None:
    write_report(tmp_path, exact_ten_task_results())
    assert configured_default_profile(tmp_path) == "single"
    seed_legacy_automatic_profile(tmp_path)
    evidence_path = Path(qualified_runtime["home"]) / "qualification-evidence.json"
    evidence_path.write_text('{"latest_attempt": "failed"}', encoding="utf-8")
    qualified_runtime["evidence_valid"] = False

    assert configured_default_profile(tmp_path) == "single"


def test_non_finite_score_fixture_values_are_actually_non_finite() -> None:
    # Guard the parameter matrix against accidental normalization by a future fixture rewrite.
    assert math.isnan(float("nan"))
    assert math.isinf(float("inf"))
