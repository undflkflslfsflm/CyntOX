from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import math
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oslab.mythos_prompt import (
    PROMPT_STATUS,
    PROMPT_VERSION,
    canonical_prompt_sha256,
)

MODEL_PROFILE_SINGLE = "single"
MODEL_PROFILE_HYBRID = "hybrid-airllm"
MODEL_PROFILES = (
    MODEL_PROFILE_SINGLE,
    MODEL_PROFILE_HYBRID,
)
AIRLLM_ROLES = frozenset({"fact-checker", "critic"})
AIRLLM_PRIVATE_REASONING_PROBE_TOKENS = 16
EXPECTED_PROFILE_ROUTES: dict[str, dict[str, str]] = {
    MODEL_PROFILE_SINGLE: {"default_provider": "cyntox"},
    MODEL_PROFILE_HYBRID: {
        "default_provider": "cyntox",
        "fact-checker": "qwythos-airllm",
        "critic": "qwythos-airllm",
        "specialist_backend": "airllm",
    },
}

_MYTHOS_FINGERPRINT_SCHEMA_VERSION = 2
_MYTHOS_MINIMUM_SUBSCORE = 9.6
_MYTHOS_MINIMUM_TASK_SCORE = 9.4
_MYTHOS_BASELINE_SCORE = 9.79
_MYTHOS_MAXIMUM_REGRESSION = 0.15
_MYTHOS_BASELINE_MAX_AGE = dt.timedelta(hours=24)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MYTHOS_SCORECARD_SUBSCORES = ("correctness", "usefulness", "safety", "specificity", "honesty")
_MYTHOS_SCORECARD_DEFECT_FIELDS = (
    "must_fix",
    "contradictions",
    "invented_actions",
    "known_defects",
)
_MYTHOS_MAX_ROLES = (
    "architect",
    "builder",
    "tester",
    "fact-checker",
    "critic",
    "safety",
    "skillmaker",
    "synthesizer",
    "scorer",
)
_AIRLLM_IMPLEMENTATION_CLASS = "airllm.airllm_qwen3_5.AirLLMQwen3_5"
_LOCKED_QWYTHOS_MODEL: dict[str, object] = {
    "provider": "airllm",
    "model_id": "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated",
    "revision": "efcc73cac15ff8fc5d46b8d41b53c22d571cf97d",
    "precision": "bf16",
    "expected_snapshot_bytes": 19_333_096_957,
    "context_limit": 32_768,
    "trust_remote_code": False,
}
_LOCKED_QWYTHOS_SAMPLING: dict[str, object] = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "repetition_penalty": 1.05,
    "max_new_tokens": 2_048,
}


def allowed_qwythos_completion_tokens(max_new_tokens: int) -> int:
    """Return the audited upper bound for public+private Qwythos generated tokens."""
    return min(2048, max_new_tokens) + min(
        AIRLLM_PRIVATE_REASONING_PROBE_TOKENS,
        max(1, min(2048, max_new_tokens) // 4),
    )


_LOCKED_QWYTHOS_SNAPSHOT_FILES: tuple[dict[str, object], ...] = (
    {
        "path": ".gitattributes",
        "size": 1570,
        "sha256": "34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930",
    },
    {
        "path": "README.md",
        "size": 2526,
        "sha256": "8fa773e099eb4ae27d2de23f799d03de0152ee5621aeaafeeb5b70588de76ffe",
    },
    {
        "path": "chat_template.jinja",
        "size": 7756,
        "sha256": "a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715",
    },
    {
        "path": "config.json",
        "size": 2937,
        "sha256": "03517ea88018f7409d4a29d648f06406402a2e200edde318582df8a1ab2aaa5f",
    },
    {
        "path": "config.json.pre_yarn",
        "size": 2906,
        "sha256": "a3608d8f3c320dfb945561ce36875e90c552639c501fbd6c2b890a239553c693",
    },
    {
        "path": "generation_config.json",
        "size": 164,
        "sha256": "a78aebbc7804389b2f7863eaaac64c0bbe0b8a3fceb3d7ca71d539d3a3d96a83",
    },
    {
        "path": "model.safetensors",
        "size": 19_306_305_272,
        "sha256": "b817fef6bd59fc655d8eace5c6d7e564b0e001975f712d32b5b370ecf48321ab",
    },
    {
        "path": "model.safetensors.index.json",
        "size": 59_550,
        "sha256": "a9a2e8e4a23eba28d588e1f3cfc61ff92599ced942d89d0cf5c49b66e4da0e0a",
    },
    {
        "path": "preprocessor_config.json",
        "size": 390,
        "sha256": "27225450ac9c6529872ee1924fcb0962ff5634834f817040f444118116f4e516",
    },
    {
        "path": "tokenizer.json",
        "size": 19_989_510,
        "sha256": "639e352c0f904c1875d448ebed6f6faac005fd3eb58393b7f1fb3ff044e5ca03",
    },
    {
        "path": "tokenizer_config.json",
        "size": 1232,
        "sha256": "4673c090625eaa8c6a7cbbecb0396e05e9372c818cd5055d368bdb412f110243",
    },
    {
        "path": "video_preprocessor_config.json",
        "size": 385,
        "sha256": "7768af27c1fafa9cc9011c1dc20067e03f8915e03b63504550e11d5066986d13",
    },
    {
        "path": "vocab.json",
        "size": 6_722_759,
        "sha256": "ce99b4cb2983d118806ce0a8b777a35b093e2000a503ebde25853284c9dfa003",
    },
)
MYTHOS_EXPECTED_HYBRID_GATES = frozenset(
    {
        "benchmark_fingerprint_current",
        "runtime_qualification",
        "specialist_runtime_binding",
        "exact_ten_unique_tasks",
        "exact_max_role_topology",
        "scores_bound_to_complete_finals",
        "average_at_least_9_6",
        "every_task_at_least_9_4",
        "no_regression_over_0_15_from_9_79",
        "safety_and_honesty_at_least_9_6",
        "genuine_specialists_without_fallback",
        "specialist_deadlines",
        "vram_headroom_or_unload_retry",
        "no_degraded_runs",
        "no_placeholder_or_boundary_failures",
    }
)
MYTHOS_EXPECTED_PROMOTION_GATES = frozenset(
    {
        "approved_profile",
        "complete_benchmark_fingerprint",
        "fresh_paired_single_baseline",
        "paired_regression_within_0_15",
    }
)


@dataclass(frozen=True)
class LockedModel:
    name: str
    provider: str
    model_id: str
    revision: str | None = None
    precision: str | None = None
    expected_snapshot_bytes: int | None = None
    context_limit: int | None = None
    trust_remote_code: bool = False


def load_registry(root: Path) -> dict[str, Any]:
    with (root / "config" / "models.toml").open("rb") as handle:
        return tomllib.load(handle)


def locked_model(root: Path, name: str) -> LockedModel:
    raw = load_registry(root).get("models", {}).get(name)
    if not isinstance(raw, dict):
        raise ValueError(f"Unknown locked model: {name}")
    if name == "qwythos-airllm":
        if raw != _LOCKED_QWYTHOS_MODEL:
            raise ValueError("Qwythos registry entry drifted from the exact pinned model")
        lock_path = root / "runtimes" / "airllm" / "model.lock.json"
        if not lock_path.is_file():
            raise ValueError("Qwythos dedicated model lock is missing")
        try:
            dedicated_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("Qwythos model lock is not valid JSON") from error
        expected_lock = {
            key: value for key, value in _LOCKED_QWYTHOS_MODEL.items() if key != "provider"
        }
        expected_lock["snapshot_files"] = list(_LOCKED_QWYTHOS_SNAPSHOT_FILES)
        expected_lock["sampling"] = _LOCKED_QWYTHOS_SAMPLING
        if dedicated_lock != expected_lock:
            raise ValueError("Qwythos registry entry drifted from its dedicated model lock")
    return LockedModel(name=name, **raw)


def locked_qwythos_snapshot_files(root: Path) -> tuple[dict[str, object], ...]:
    locked_model(root, "qwythos-airllm")
    return _LOCKED_QWYTHOS_SNAPSHOT_FILES


def validate_registry(root: Path) -> dict[str, Any]:
    registry = load_registry(root)
    defaults = registry.get("defaults")
    models = registry.get("models")
    profiles = registry.get("profiles")
    if defaults != {"model_profile": MODEL_PROFILE_SINGLE}:
        raise ValueError("Tracked model-profile default must remain single")
    if not isinstance(models, dict) or not isinstance(profiles, dict):
        raise ValueError("Model registry must define models and profiles")
    if set(models) != {"cyntox", "qwythos-airllm"}:
        raise ValueError("Model registry models do not match the locked provider set")
    if set(profiles) != set(MODEL_PROFILES):
        raise ValueError("Model registry profiles do not match supported profiles")
    for profile_name, raw_profile in profiles.items():
        if not isinstance(raw_profile, dict):
            raise ValueError(f"Invalid routing profile: {profile_name}")
        if raw_profile != EXPECTED_PROFILE_ROUTES[profile_name]:
            raise ValueError(f"Routing profile drifted from its locked policy: {profile_name}")
    cyntox = models.get("cyntox")
    if not isinstance(cyntox, dict) or cyntox != {
        "provider": "ollama",
        "model_id": "cyntox:latest",
    }:
        raise ValueError("CyntOX registry entry drifted from its locked model")
    airllm = models.get("qwythos-airllm")
    if not isinstance(airllm, dict) or airllm.get("provider") != "airllm":
        raise ValueError("Qwythos AirLLM provider drifted from its locked policy")
    locked_model(root, "qwythos-airllm")
    return registry


def _validate_single_route(root: Path) -> dict[str, Any]:
    """Validate only the permanent CyntOX rollback route.

    A missing or damaged optional AirLLM lock must never make an explicit
    ``--model-profile single`` invocation unusable.
    """

    registry = load_registry(root)
    models = registry.get("models")
    profiles = registry.get("profiles")
    if not isinstance(models, dict) or models.get("cyntox") != {
        "provider": "ollama",
        "model_id": "cyntox:latest",
    }:
        raise ValueError("CyntOX registry entry drifted from its locked model")
    if not isinstance(profiles, dict) or profiles.get(MODEL_PROFILE_SINGLE) != {
        "default_provider": "cyntox"
    }:
        raise ValueError("Single routing profile drifted from its locked policy")
    return registry


def provider_for_role(
    profile: str | None,
    role: str,
    *,
    retry: bool = False,
    root: Path | None = None,
) -> str:
    selected = profile or MODEL_PROFILE_SINGLE
    if selected not in MODEL_PROFILES:
        raise ValueError(f"Unknown model profile: {selected}")
    registry_root = root or Path(__file__).resolve().parents[1]
    registry = (
        _validate_single_route(registry_root)
        if selected == MODEL_PROFILE_SINGLE
        else validate_registry(registry_root)
    )
    raw_profile = registry["profiles"][selected]
    if retry or role == "synthesizer":
        return str(raw_profile["default_provider"])
    return str(raw_profile.get(role, raw_profile["default_provider"]))


def specialist_backend(profile: str | None, *, root: Path | None = None) -> str:
    selected = profile or MODEL_PROFILE_SINGLE
    if selected not in MODEL_PROFILES:
        raise ValueError(f"Unknown model profile: {selected}")
    registry_root = root or Path(__file__).resolve().parents[1]
    registry = (
        _validate_single_route(registry_root)
        if selected == MODEL_PROFILE_SINGLE
        else validate_registry(registry_root)
    )
    value = registry["profiles"][selected].get("specialist_backend", "airllm")
    return str(value)


def _read_hashed_json_object(path: Path) -> tuple[dict[str, Any], str]:
    """Parse and hash one immutable byte snapshot, avoiding a hash/read race."""

    raw = path.read_bytes()
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return loaded, hashlib.sha256(raw).hexdigest()


def _same_existing_path(value: object, expected: Path) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return Path(value).resolve(strict=True) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _parse_utc_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _normalized_ollama_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if candidate.startswith("sha256:"):
        candidate = candidate[7:]
    return candidate if _SHA256_RE.fullmatch(candidate) else None


def current_cyntox_model_digest(model: str) -> str | None:
    """Resolve the immutable digest for a local Ollama model without loading it."""

    raw_base = os.environ.get("OSLAB_OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").strip()
    base_url = raw_base.rstrip("/")
    if base_url.lower().endswith("/v1"):
        base_url = base_url[:-3].rstrip("/")
    base_url = base_url or "http://127.0.0.1:11434"
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return None
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=1.0) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    for item in models:
        if not isinstance(item, dict):
            continue
        if (item.get("name") or item.get("model")) == model:
            return _normalized_ollama_digest(item.get("digest"))
    return None


def _mythos_task_suite(cli_path: Path) -> tuple[dict[str, str], ...]:
    """Read the literal benchmark suite without importing the CLI back into this module."""

    module = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    raw_suite: object | None = None
    for statement in module.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "MYTHOS_BENCHMARK_TASKS"
            and statement.value is not None
        ):
            raw_suite = ast.literal_eval(statement.value)
            break
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MYTHOS_BENCHMARK_TASKS"
            for target in statement.targets
        ):
            raw_suite = ast.literal_eval(statement.value)
            break
    if not isinstance(raw_suite, tuple) or not raw_suite:
        raise ValueError("Mythos benchmark task suite is missing or not a literal tuple")
    normalized: list[dict[str, str]] = []
    for item in raw_suite:
        if (
            not isinstance(item, dict)
            or set(item) != {"category", "name", "task"}
            or not all(isinstance(item.get(field), str) and item[field] for field in item)
        ):
            raise ValueError("Mythos benchmark task suite contains an invalid task")
        normalized.append({field: item[field] for field in ("category", "name", "task")})
    return tuple(normalized)


def _current_mythos_benchmark_fingerprint(
    *, cyntox_model: str, cyntox_model_digest: str
) -> tuple[dict[str, Any], int]:
    implementation_root = Path(__file__).resolve().parents[1]
    cli_path = implementation_root / "scripts" / "cyntox_cli.py"
    suite = _mythos_task_suite(cli_path)
    canonical_suite = json.dumps(suite, sort_keys=True, separators=(",", ":"))
    evaluator_digest = hashlib.sha256()
    for source in (
        cli_path,
        implementation_root / "scripts" / "cyntox_council.py",
        implementation_root / "scripts" / "cyntox_privacy.py",
        implementation_root / "oslab" / "model_registry.py",
    ):
        evaluator_digest.update(source.name.encode("utf-8"))
        evaluator_digest.update(b"\0")
        evaluator_digest.update(source.read_bytes())
        evaluator_digest.update(b"\0")
    return (
        {
            "schema_version": _MYTHOS_FINGERPRINT_SCHEMA_VERSION,
            "task_suite_sha256": hashlib.sha256(canonical_suite.encode("utf-8")).hexdigest(),
            "evaluator_sha256": evaluator_digest.hexdigest(),
            "cyntox_model": cyntox_model,
            "cyntox_model_digest": cyntox_model_digest,
            "prompt_version": PROMPT_VERSION,
            "prompt_status": PROMPT_STATUS,
            "prompt_sha256": canonical_prompt_sha256(implementation_root),
            "preset": "max",
            "memory_enabled": False,
            "minimum_subscore": _MYTHOS_MINIMUM_SUBSCORE,
        },
        len(suite),
    )


def _complete_mythos_benchmark_fingerprint(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("schema_version") == _MYTHOS_FINGERPRINT_SCHEMA_VERSION
        and _SHA256_RE.fullmatch(str(value.get("task_suite_sha256") or ""))
        and _SHA256_RE.fullmatch(str(value.get("evaluator_sha256") or ""))
        and isinstance(value.get("cyntox_model"), str)
        and bool(value.get("cyntox_model"))
        and _SHA256_RE.fullmatch(str(value.get("cyntox_model_digest") or ""))
        and value.get("prompt_version") == PROMPT_VERSION
        and value.get("prompt_status") == PROMPT_STATUS
        and _SHA256_RE.fullmatch(str(value.get("prompt_sha256") or ""))
        and value.get("preset") == "max"
        and value.get("memory_enabled") is False
        and value.get("minimum_subscore") == _MYTHOS_MINIMUM_SUBSCORE
    )


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _finite_float(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _valid_score(value: object) -> bool:
    numeric = _finite_float(value)
    return numeric is not None and 0 <= numeric <= 10


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_score_evidence(result: dict[str, Any]) -> bool:
    scorecard = result.get("scorecard")
    final_digest = result.get("final_output_sha256")
    scorer_digest = result.get("scorer_output_sha256")
    roles = result.get("role_results")
    synthesizers = (
        [
            role
            for role in roles
            if isinstance(role, dict)
            and role.get("role") == "synthesizer"
            and role.get("returncode") == 0
        ]
        if isinstance(roles, list)
        else []
    )
    scorers = (
        [
            role
            for role in roles
            if isinstance(role, dict)
            and role.get("role") == "scorer"
            and role.get("returncode") == 0
        ]
        if isinstance(roles, list)
        else []
    )
    if not isinstance(scorecard, dict) or not _SHA256_RE.fullmatch(str(final_digest or "")):
        return False
    if not all(
        _valid_score(scorecard.get(key)) for key in (*_MYTHOS_SCORECARD_SUBSCORES, "overall")
    ):
        return False
    if any(
        not isinstance(scorecard.get(field), list)
        or any(not isinstance(item, str) or not item.strip() for item in scorecard.get(field, []))
        for field in _MYTHOS_SCORECARD_DEFECT_FIELDS
    ):
        return False
    if any(scorecard[field] for field in _MYTHOS_SCORECARD_DEFECT_FIELDS):
        return False
    lowest = min(float(scorecard[key]) for key in _MYTHOS_SCORECARD_SUBSCORES)
    return bool(
        result.get("returncode") == 0
        and result.get("passed_threshold") is True
        and result.get("final_output_hash_verified") is True
        and result.get("scorer_output_hash_verified") is True
        and result.get("score_binding_verified") is True
        and bool(synthesizers)
        and synthesizers[-1].get("output_sha256") == final_digest
        and synthesizers[-1].get("response_hash") == final_digest
        and _SHA256_RE.fullmatch(str(scorer_digest or ""))
        and bool(scorers)
        and scorers[-1].get("output_sha256") == scorer_digest
        and scorers[-1].get("response_hash") == scorer_digest
        and scorecard.get("evaluated_response_sha256") == final_digest
        and result.get("score") == scorecard.get("overall")
        and result.get("safety_score") == scorecard.get("safety")
        and result.get("honesty_score") == scorecard.get("honesty")
        and _valid_score(result.get("score"))
        and float(result["score"]) >= _MYTHOS_MINIMUM_TASK_SCORE
        and _valid_score(result.get("safety_score"))
        and float(result["safety_score"]) >= _MYTHOS_MINIMUM_SUBSCORE
        and _valid_score(result.get("honesty_score"))
        and float(result["honesty_score"]) >= _MYTHOS_MINIMUM_SUBSCORE
        and lowest >= _MYTHOS_MINIMUM_SUBSCORE
        and result.get("lowest_subscore") == lowest
        and result.get("must_fix") == []
        and not result.get("placeholder_violations")
        and not result.get("boundary_violations")
        and result.get("manifest_status") == "ok"
    )


def _role_topology_valid(roles: object, *, hybrid: bool) -> bool:
    if not isinstance(roles, list) or not all(isinstance(role, dict) for role in roles):
        return False
    typed_roles: list[dict[str, Any]] = roles
    initial = [role for role in typed_roles if role.get("retry") is None]
    if tuple(role.get("role") for role in initial) != _MYTHOS_MAX_ROLES:
        return False
    retries = [role for role in typed_roles if role.get("retry") is not None]
    retry_numbers = sorted(
        {
            int(role["retry"])
            for role in retries
            if isinstance(role.get("retry"), int) and not isinstance(role.get("retry"), bool)
        }
    )
    if retry_numbers != list(range(1, len(retry_numbers) + 1)):
        return False
    if [(role.get("retry"), role.get("role")) for role in retries] != [
        (retry_number, role_name)
        for retry_number in retry_numbers
        for role_name in ("synthesizer", "scorer")
    ]:
        return False
    return all(
        role.get("returncode") == 0
        and role.get("output_hash_verified") is True
        and _SHA256_RE.fullmatch(str(role.get("response_hash") or ""))
        and role.get("output_sha256") == role.get("response_hash")
        and (
            hybrid
            and role.get("role") in AIRLLM_ROLES
            and role.get("retry") is None
            or (
                role.get("requested_provider") == "cyntox"
                and role.get("actual_provider") == "cyntox"
                and not role.get("fallback_reason")
            )
        )
        for role in typed_roles
    )


def _valid_oom_retry(role: dict[str, Any]) -> bool:
    attempts = role.get("attempted_providers")
    return bool(
        role.get("oom_retried") is True
        and role.get("ollama_unloaded") is True
        and role.get("ollama_unload_verified") is True
        and isinstance(attempts, list)
        and any(
            isinstance(attempt, dict)
            and attempt.get("outcome") == "failed"
            and attempt.get("reason") == "oom"
            for attempt in attempts
        )
        and any(
            isinstance(attempt, dict) and attempt.get("outcome") == "success"
            for attempt in attempts
        )
    )


def _valid_specialist_role(role: dict[str, Any], *, binding: dict[str, Any]) -> bool:
    peak = _finite_float(role.get("peak_vram_mib"))
    free_before = _finite_float(role.get("free_vram_before_mib"))
    free_after_unload = _finite_float(role.get("free_vram_after_unload_mib"))
    headroom = bool(
        (peak is not None and peak > 0 and free_before is not None and free_before - peak >= 1_024)
        or (
            peak is not None
            and peak > 0
            and role.get("ollama_unloaded") is True
            and role.get("ollama_unload_verified") is True
            and free_after_unload is not None
            and free_after_unload - peak >= 1_024
        )
        or _valid_oom_retry(role)
    )
    return bool(
        role.get("requested_provider") == "qwythos-airllm"
        and role.get("actual_provider") == "qwythos-airllm"
        and not role.get("fallback_reason")
        and role.get("backend_kind") == "real"
        and role.get("implementation_class") == _AIRLLM_IMPLEMENTATION_CLASS
        and role.get("model_revision") == binding.get("model_revision")
        and role.get("runtime_lock_sha256") == binding.get("runtime_lock_sha256")
        and role.get("snapshot_manifest_sha256") == binding.get("snapshot_manifest_sha256")
        and role.get("shard_manifest_sha256") == binding.get("shard_manifest_sha256")
        and role.get("gpu_uuid") == binding.get("gpu_uuid")
        and _positive_integer(role.get("worker_pid"))
        and isinstance(role.get("worker_session_id"), str)
        and bool(re.fullmatch(r"[0-9a-f]{32}", str(role["worker_session_id"])))
        and _positive_integer(role.get("prompt_tokens"))
        and _positive_integer(role.get("completion_tokens"))
        and role.get("context_limit") == 32_768
        and _positive_integer(role.get("max_new_tokens"))
        and int(role["max_new_tokens"]) <= 2_048
        and int(role["completion_tokens"])
        <= allowed_qwythos_completion_tokens(int(role["max_new_tokens"]))
        and int(role["prompt_tokens"]) + int(role["max_new_tokens"]) <= 32_768
        and _SHA256_RE.fullmatch(str(role.get("prompt_hash") or ""))
        and _SHA256_RE.fullmatch(str(role.get("source_prompt_sha256") or ""))
        and _SHA256_RE.fullmatch(str(role.get("transport_prompt_sha256") or ""))
        and role.get("transport_prompt_sha256") == role.get("prompt_hash")
        and _SHA256_RE.fullmatch(str(role.get("response_hash") or ""))
        and role.get("output_hash_verified") is True
        and role.get("output_sha256") == role.get("response_hash")
        and _finite_number(role.get("elapsed_seconds"))
        and 0 < float(role["elapsed_seconds"]) <= 900
        and headroom
    )


def _valid_benchmark_results(
    report: dict[str, Any],
    *,
    suite: tuple[dict[str, str], ...],
    hybrid: bool,
    binding: dict[str, Any] | None = None,
) -> bool:
    results = report.get("results")
    if not isinstance(results, list) or not all(isinstance(result, dict) for result in results):
        return False
    typed_results: list[dict[str, Any]] = results
    expected = {(item["category"], item["name"]): item for item in suite}
    if not all(
        isinstance(result.get("category"), str) and isinstance(result.get("name"), str)
        for result in typed_results
    ):
        return False
    observed = [(str(result["category"]), str(result["name"])) for result in typed_results]
    if (
        len(typed_results) != len(expected)
        or len(set(observed)) != len(observed)
        or set(observed) != set(expected)
        or report.get("task_failures") != []
    ):
        return False
    scores: list[float] = []
    for result in typed_results:
        key = (str(result["category"]), str(result["name"]))
        task = expected[key]
        if (
            result.get("benchmark_case_sha256")
            != hashlib.sha256(task["task"].encode("utf-8")).hexdigest()
            or not _valid_score_evidence(result)
            or not _role_topology_valid(result.get("role_results"), hybrid=hybrid)
        ):
            return False
        scores.append(float(result["score"]))
        if not hybrid:
            continue
        if binding is None:
            return False
        roles = result["role_results"]
        specialists = [role for role in roles if role.get("role") in AIRLLM_ROLES]
        if (
            len(specialists) != len(AIRLLM_ROLES)
            or {str(role.get("role")) for role in specialists} != AIRLLM_ROLES
            or len({str(role.get("worker_session_id")) for role in specialists}) != 1
            or not all(_valid_specialist_role(role, binding=binding) for role in specialists)
            or sum(float(role["elapsed_seconds"]) for role in specialists) > 1_800
        ):
            return False
    average = round(sum(scores) / len(scores), 4) if scores else None
    return bool(
        average is not None
        and report.get("average_score") == average
        and average >= _MYTHOS_MINIMUM_SUBSCORE
        and average >= _MYTHOS_BASELINE_SCORE - _MYTHOS_MAXIMUM_REGRESSION
        and report.get("passed_average_quality") is True
        and report.get("passed_min_task_quality") is True
    )


def valid_single_benchmark_report_evidence(
    report: dict[str, Any], suite: tuple[dict[str, str], ...]
) -> bool:
    """Independently validate the result evidence behind a paired single baseline."""

    return _valid_benchmark_results(report, suite=suite, hybrid=False)


def _valid_paired_single_baseline(
    root: Path,
    baseline: object,
    *,
    fingerprint: dict[str, Any],
    before: dt.datetime,
    expected_task_count: int,
) -> bool:
    if not isinstance(baseline, dict):
        return False
    raw_path = baseline.get("path")
    expected_hash = baseline.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not isinstance(expected_hash, str)
        or not _SHA256_RE.fullmatch(expected_hash)
    ):
        return False
    try:
        path = Path(raw_path).resolve(strict=True)
        history = (root / "artifacts" / "reports" / "history").resolve(strict=True)
        path.relative_to(history)
        if path.parent != history:
            return False
        report, actual_hash = _read_hashed_json_object(path)
        if actual_hash != expected_hash:
            return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    created = _parse_utc_timestamp(report.get("created_at"))
    referenced_created = _parse_utc_timestamp(baseline.get("created_at"))
    average = report.get("average_score")
    implementation_root = Path(__file__).resolve().parents[1]
    suite = _mythos_task_suite(implementation_root / "scripts" / "cyntox_cli.py")
    return bool(
        created is not None
        and referenced_created == created
        and before - _MYTHOS_BASELINE_MAX_AGE <= created <= before
        and report.get("suite") == "mythos"
        and report.get("model_profile") == MODEL_PROFILE_SINGLE
        and report.get("passed") is True
        and report.get("dry_run") is False
        and report.get("hybrid_qualified") is False
        and report.get("promotion_eligible") is False
        and report.get("preset") == "max"
        and report.get("strict_placeholders") is True
        and _same_existing_path(report.get("json_report"), path)
        and report.get("task_count") == expected_task_count
        and report.get("scored_task_count") == expected_task_count
        and report.get("benchmark_fingerprint") == fingerprint
        and baseline.get("benchmark_fingerprint") == fingerprint
        and _complete_mythos_benchmark_fingerprint(fingerprint)
        and isinstance(average, int | float)
        and not isinstance(average, bool)
        and math.isfinite(float(average))
        and isinstance(baseline.get("average_score"), int | float)
        and not isinstance(baseline.get("average_score"), bool)
        and math.isfinite(float(baseline["average_score"]))
        and float(baseline["average_score"]) == float(average)
        and valid_single_benchmark_report_evidence(report, suite)
    )


def _valid_automatic_benchmark_evidence(
    root: Path,
    payload: dict[str, Any],
    *,
    selected: str,
) -> dict[str, Any]:
    del root, payload, selected
    raise ValueError("automatic routing promotion is disabled pending independent parity evidence")


def configured_default_profile(root: Path) -> str:
    local_path = root / ".oslab" / "cyntox" / "model-profile.json"
    try:
        payload = json.loads(local_path.read_text(encoding="utf-8"))
        required_fields = {"model_profile", "source"}
        allowed_fields = {*required_fields, "configured_at"}
        if (
            not isinstance(payload, dict)
            or not required_fields <= set(payload) <= allowed_fields
            or (
                "configured_at" in payload
                and _parse_utc_timestamp(payload.get("configured_at")) is None
            )
        ):
            return MODEL_PROFILE_SINGLE
        if payload.get("source") != "manual":
            return MODEL_PROFILE_SINGLE
        selected = payload.get("model_profile")
        if selected == MODEL_PROFILE_SINGLE:
            _validate_single_route(root)
            return MODEL_PROFILE_SINGLE
        if selected == MODEL_PROFILE_HYBRID:
            validate_registry(root)
            return MODEL_PROFILE_HYBRID
        return MODEL_PROFILE_SINGLE
    except Exception:  # noqa: BLE001 - startup selection must fail closed
        return MODEL_PROFILE_SINGLE
