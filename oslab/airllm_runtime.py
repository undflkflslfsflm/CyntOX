from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oslab.gpu_lease import GpuLease, default_gpu_lease_path, selected_gpu_uuid
from oslab.model_registry import (
    allowed_qwythos_completion_tokens,
    locked_model,
    locked_qwythos_snapshot_files,
)
from oslab.resource_lease import ResourceActivityLease

MIN_FREE_BYTES = 50 * 1024**3
ENV_HOME = "CYNTOX_AIRLLM_HOME"
SAFE_ENV_NAMES = {
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}
OPTIONAL_KERNEL_MODULES = ("causal_conv1d", "fla", "flash_attn")
QUALIFICATION_BINDING_SCHEMA_VERSION = 3
QUALIFICATION_RECORD_SCHEMA_VERSION = 3
QUALIFICATION_EVIDENCE_SCHEMA_VERSION = 3
REQUIRED_QUALIFICATION_REPETITIONS = 3
QUALIFICATION_CHECKS = ("lifecycle_cleanup", "failure_fallback", "no_egress")
AIRLLM_SMOKE_MAX_NEW_TOKENS = 64
RESIDENT_SMOKE_MAX_NEW_TOKENS = 2_048
AIRLLM_SMOKE_SEED = 12
RESIDENT_SMOKE_SEED = 0
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_SESSION_RE = re.compile(r"[0-9a-f]{32}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# Qualification is valid only for the exact implementation that produced it.
# Keep these names stable and bump QUALIFICATION_BINDING_SCHEMA_VERSION whenever
# the shape or meaning of this set changes.
QUALIFICATION_IMPLEMENTATION_PATHS: tuple[tuple[str, str], ...] = (
    ("airllm_worker", "scripts/cyntox_airllm_worker.py"),
    ("airllm_provider", "oslab/model/airllm.py"),
    ("council_orchestration", "scripts/cyntox_council.py"),
    ("model_registry", "oslab/model_registry.py"),
    ("model_registry_config", "config/models.toml"),
    ("worker_protocol_parser", "oslab/airllm_protocol.py"),
    ("qualification_harness", "oslab/airllm_qualification.py"),
    ("qualification_runtime", "oslab/airllm_runtime.py"),
    ("qualification_promotion", "scripts/cyntox_cli.py"),
    ("generation_lease", "oslab/generation_lease.py"),
    ("gpu_lease", "oslab/gpu_lease.py"),
    ("resource_lease", "oslab/resource_lease.py"),
    ("safe_process_runner", "oslab/process_runner.py"),
)
_BACKEND_SPECS: dict[str, dict[str, object]] = {
    "airllm": {
        "implementation_class": "airllm.airllm_qwen3_5.AirLLMQwen3_5",
        "context_limit": 32_768,
    },
    "transformers-resident": {
        "implementation_class": ("transformers.models.qwen3_5.modeling_qwen3_5.Qwen3_5ForCausalLM"),
        "context_limit": 8_192,
    },
}


@dataclass(frozen=True)
class QualificationSmokeSpec:
    backend: str
    backend_name: str
    prompt: str
    response: str
    prompt_hash: str
    response_hash: str
    max_new_tokens: int
    seed: int


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _qualification_smoke_spec(
    *,
    backend: str,
    backend_name: str,
    max_new_tokens: int,
    seed: int,
) -> QualificationSmokeSpec:
    prompt = f"Reply with exactly: Qwythos {backend} ready"
    response = f"Qwythos {backend} ready"
    # AirLlmProvider serializes a single user message as `user: <content>` before
    # hashing it. Binding the record to that exact transport prompt makes format
    # drift invalidate old qualification evidence instead of silently reusing it.
    transport_prompt = f"user: {prompt}"
    return QualificationSmokeSpec(
        backend=backend,
        backend_name=backend_name,
        prompt=prompt,
        response=response,
        prompt_hash=_text_sha256(transport_prompt),
        response_hash=_text_sha256(response),
        max_new_tokens=max_new_tokens,
        seed=seed,
    )


_QUALIFICATION_SMOKE_SPECS = {
    "airllm": _qualification_smoke_spec(
        backend="airllm",
        backend_name="airllm",
        max_new_tokens=AIRLLM_SMOKE_MAX_NEW_TOKENS,
        seed=AIRLLM_SMOKE_SEED,
    ),
    "transformers-resident": _qualification_smoke_spec(
        backend="resident",
        backend_name="transformers-resident",
        max_new_tokens=RESIDENT_SMOKE_MAX_NEW_TOKENS,
        seed=RESIDENT_SMOKE_SEED,
    ),
}
QUALIFICATION_EVIDENCE_DETAILS = {
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
}


def runtime_home() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is unavailable; set CYNTOX_AIRLLM_HOME")
    return Path(local) / "CyntOX" / "airllm"


def runtime_python(home: Path | None = None) -> Path:
    base = home or runtime_home()
    return base / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _load_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _finite_number(value: object, *, maximum: float) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 < float(value) <= maximum
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def current_qualification_binding(root: Path, *, home: Path | None = None) -> dict[str, Any]:
    """Return the exact code, config, lock, and runtime identity qualification attests."""
    runtime = home or runtime_home()
    model = locked_model(root, "qwythos-airllm")

    def hash_if_file(path: Path) -> str | None:
        return manifest_hash(path) if path.is_file() else None

    return {
        "schema_version": QUALIFICATION_BINDING_SCHEMA_VERSION,
        "model_id": model.model_id,
        "model_revision": model.revision,
        "precision": model.precision,
        "model_context_limit": model.context_limit,
        "implementation_sha256": {
            name: hash_if_file(root / relative_path)
            for name, relative_path in QUALIFICATION_IMPLEMENTATION_PATHS
        },
        "model_lock_sha256": hash_if_file(root / "runtimes" / "airllm" / "model.lock.json"),
        "runtime_lock_sha256": hash_if_file(root / "runtimes" / "airllm" / "requirements.lock.txt"),
        "snapshot_manifest_sha256": hash_if_file(runtime / "snapshot-manifest.json"),
        "shard_manifest_sha256": hash_if_file(runtime / "shard-manifest.json"),
        "gpu_uuid": selected_gpu_uuid(),
    }


def qualification_binding_sha256(binding: dict[str, Any]) -> str:
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binding_complete(binding: object) -> bool:
    if not isinstance(binding, dict):
        return False
    implementation = binding.get("implementation_sha256")
    expected_implementation_names = {name for name, _path in QUALIFICATION_IMPLEMENTATION_PATHS}
    return bool(
        binding.get("schema_version") == QUALIFICATION_BINDING_SCHEMA_VERSION
        and isinstance(binding.get("model_id"), str)
        and isinstance(binding.get("model_revision"), str)
        and binding.get("precision") == "bf16"
        and _positive_integer(binding.get("model_context_limit"))
        and isinstance(binding.get("gpu_uuid"), str)
        and bool(str(binding["gpu_uuid"]).strip())
        and isinstance(implementation, dict)
        and set(implementation) == expected_implementation_names
        and all(_valid_digest(value) for value in implementation.values())
        and all(
            _valid_digest(binding.get(field))
            for field in (
                "model_lock_sha256",
                "runtime_lock_sha256",
                "snapshot_manifest_sha256",
                "shard_manifest_sha256",
            )
        )
    )


def qualification_binding_is_complete(binding: object) -> bool:
    return _binding_complete(binding)


def qualification_smoke_spec(backend_name: str) -> QualificationSmokeSpec:
    """Return the immutable prompt, result, seed, and budget for a smoke backend."""
    try:
        return _QUALIFICATION_SMOKE_SPECS[backend_name]
    except KeyError as error:
        raise ValueError(f"unsupported Qwythos qualification backend: {backend_name}") from error


def valid_qualification_record(
    qualification: object,
    *,
    binding: dict[str, Any],
    backend_name: str,
) -> bool:
    """Validate a live smoke measurement against the currently prepared runtime."""
    if not isinstance(qualification, dict) or not _binding_complete(binding):
        return False
    spec = _BACKEND_SPECS.get(backend_name)
    if spec is None:
        return False
    smoke_spec = qualification_smoke_spec(backend_name)
    context_limit = spec["context_limit"]
    if not isinstance(context_limit, int):
        return False
    prompt_tokens = qualification.get("prompt_tokens")
    completion_tokens = qualification.get("completion_tokens")
    max_new_tokens = qualification.get("max_new_tokens")
    started = _parse_timestamp(qualification.get("started_at"))
    ended = _parse_timestamp(qualification.get("ended_at"))
    created = _parse_timestamp(qualification.get("created_at"))
    timestamps_valid = bool(
        started is not None
        and ended is not None
        and created is not None
        and started <= ended <= created
        and (ended - started).total_seconds() <= 905
        and (created - ended).total_seconds() <= 300
    )
    elapsed = qualification.get("elapsed_seconds")
    startup_peak = qualification.get("startup_peak_vram_mib")
    peak = qualification.get("peak_vram_mib")
    numeric_values = (
        prompt_tokens,
        completion_tokens,
        max_new_tokens,
        elapsed,
        startup_peak,
        peak,
    )
    if any(
        not isinstance(value, int | float) or isinstance(value, bool) for value in numeric_values
    ):
        return False
    assert isinstance(prompt_tokens, int | float)
    assert isinstance(completion_tokens, int | float)
    assert isinstance(max_new_tokens, int | float)
    assert isinstance(elapsed, int | float)
    assert isinstance(startup_peak, int | float)
    assert isinstance(peak, int | float)
    wall_seconds = (ended - started).total_seconds() if started and ended else 0.0
    return bool(
        qualification.get("schema_version") == QUALIFICATION_RECORD_SCHEMA_VERSION
        and qualification.get("attempt_status") == "passed"
        and qualification.get("passed") is True
        and qualification.get("hashes_verified") is True
        and qualification.get("model_id") == binding["model_id"]
        and qualification.get("model_revision") == binding["model_revision"]
        and qualification.get("architecture") == "Qwen3_5ForConditionalGeneration"
        and qualification.get("backend_kind") == "real"
        and qualification.get("backend") == smoke_spec.backend
        and qualification.get("backend_name") == backend_name
        and qualification.get("implementation_class") == spec["implementation_class"]
        and qualification.get("context_limit") == context_limit
        and qualification.get("qualification_binding") == binding
        and qualification.get("qualification_binding_sha256")
        == qualification_binding_sha256(binding)
        and qualification.get("model_lock_sha256") == binding["model_lock_sha256"]
        and qualification.get("runtime_lock_sha256") == binding["runtime_lock_sha256"]
        and qualification.get("snapshot_manifest_sha256") == binding["snapshot_manifest_sha256"]
        and qualification.get("shard_manifest_sha256") == binding["shard_manifest_sha256"]
        and qualification.get("gpu_uuid") == binding["gpu_uuid"]
        and _positive_integer(qualification.get("worker_pid"))
        and isinstance(qualification.get("worker_session_id"), str)
        and _SESSION_RE.fullmatch(str(qualification["worker_session_id"])) is not None
        and _positive_integer(prompt_tokens)
        and _positive_integer(completion_tokens)
        and _positive_integer(max_new_tokens)
        and int(max_new_tokens) == smoke_spec.max_new_tokens
        and int(completion_tokens) <= allowed_qwythos_completion_tokens(int(max_new_tokens))
        and int(prompt_tokens) + int(max_new_tokens) <= context_limit
        and qualification.get("prompt_hash") == smoke_spec.prompt_hash
        and qualification.get("response_hash") == smoke_spec.response_hash
        and isinstance(qualification.get("seed"), int)
        and not isinstance(qualification.get("seed"), bool)
        and int(qualification["seed"]) >= 0
        and qualification.get("seed") == smoke_spec.seed
        and _finite_number(elapsed, maximum=900)
        and float(elapsed) <= wall_seconds + 5
        and isinstance(startup_peak, int | float)
        and not isinstance(startup_peak, bool)
        and math.isfinite(float(startup_peak))
        and 0 <= float(startup_peak) <= 1_000_000
        and _finite_number(peak, maximum=1_000_000)
        and float(peak) >= float(startup_peak)
        and timestamps_valid
    )


def qualification_evidence_path(home: Path | None = None) -> Path:
    return (home or runtime_home()) / "qualification-evidence.json"


def _valid_evidence_record(
    record: object,
    *,
    binding: dict[str, Any],
    backend_name: str,
) -> bool:
    if not isinstance(record, dict):
        return False
    spec = _BACKEND_SPECS[backend_name]
    check = record.get("check")
    if check not in QUALIFICATION_CHECKS:
        return False
    details = record.get("details")
    sessions = record.get("worker_session_ids")
    return bool(
        record.get("schema_version") == QUALIFICATION_EVIDENCE_SCHEMA_VERSION
        and record.get("attempt_status") == "passed"
        and record.get("passed") is True
        and record.get("live") is True
        and record.get("hashes_verified") is True
        and record.get("backend_kind") == "real"
        and record.get("backend_name") == backend_name
        and record.get("implementation_class") == spec["implementation_class"]
        and isinstance(record.get("run_id"), str)
        and _RUN_ID_RE.fullmatch(str(record["run_id"])) is not None
        and _parse_timestamp(record.get("created_at")) is not None
        and _finite_number(record.get("elapsed_seconds"), maximum=7_200)
        and isinstance(sessions, list)
        and bool(sessions)
        and len(sessions) == len(set(sessions))
        and all(isinstance(item, str) and _SESSION_RE.fullmatch(item) for item in sessions)
        and record.get("qualification_binding") == binding
        and record.get("qualification_binding_sha256") == qualification_binding_sha256(binding)
        and isinstance(details, dict)
        and all(details.get(field) is True for field in QUALIFICATION_EVIDENCE_DETAILS[str(check)])
    )


def qualification_evidence_status(
    root: Path,
    *,
    backend_name: str,
    home: Path | None = None,
    evidence: object | None = None,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if backend_name not in _BACKEND_SPECS:
        raise ValueError(f"unsupported qualification backend: {backend_name}")
    runtime = home or runtime_home()
    current = binding or current_qualification_binding(root, home=runtime)
    payload = (
        evidence if evidence is not None else _load_object(qualification_evidence_path(runtime))
    )
    file_valid = bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == QUALIFICATION_EVIDENCE_SCHEMA_VERSION
        and payload.get("qualification_binding") == current
        and payload.get("qualification_binding_sha256") == qualification_binding_sha256(current)
        and isinstance(payload.get("records"), list)
        and _binding_complete(current)
    )
    records = payload.get("records", []) if file_valid and isinstance(payload, dict) else []
    applicable_attempts = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("backend_name") == backend_name
        and record.get("backend_kind") == "real"
        and record.get("live") is True
        and record.get("qualification_binding") == current
        and record.get("qualification_binding_sha256") == qualification_binding_sha256(current)
    ]
    valid_records = [
        record
        for record in applicable_attempts
        if _valid_evidence_record(record, binding=current, backend_name=backend_name)
    ]
    checks: dict[str, Any] = {}
    passing_run_ids_by_check: dict[str, set[str]] = {}
    for check in QUALIFICATION_CHECKS:
        matching = [record for record in valid_records if record.get("check") == check]
        matching_attempts = [
            record for record in applicable_attempts if record.get("check") == check
        ]
        latest_attempt_passed = bool(
            matching_attempts
            and _valid_evidence_record(
                matching_attempts[-1], binding=current, backend_name=backend_name
            )
        )
        run_ids = {str(record["run_id"]) for record in matching}
        passing_run_ids_by_check[check] = run_ids
        sessions = {
            str(session) for record in matching for session in record.get("worker_session_ids", [])
        }
        passed = (
            latest_attempt_passed
            and len(matching) >= REQUIRED_QUALIFICATION_REPETITIONS
            and len(run_ids) >= REQUIRED_QUALIFICATION_REPETITIONS
            and len(sessions) >= REQUIRED_QUALIFICATION_REPETITIONS
        )
        checks[check] = {
            "passed": passed,
            "latest_attempt_passed": latest_attempt_passed,
            "valid_repetitions": len(matching),
            "distinct_runs": len(run_ids),
            "distinct_sessions": len(sessions),
            "required_repetitions": REQUIRED_QUALIFICATION_REPETITIONS,
        }
    complete_run_ids = set.intersection(
        *(passing_run_ids_by_check[check] for check in QUALIFICATION_CHECKS)
    )
    complete_repetitions = len(complete_run_ids)
    return {
        "schema_version": QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
        "path": str(qualification_evidence_path(runtime)),
        "backend_name": backend_name,
        "file_valid": file_valid,
        "valid": file_valid
        and complete_repetitions >= REQUIRED_QUALIFICATION_REPETITIONS
        and all(check["passed"] for check in checks.values()),
        "checks": checks,
        "complete_repetitions": complete_repetitions,
        "required_complete_repetitions": REQUIRED_QUALIFICATION_REPETITIONS,
        "record_count": len(records),
        "valid_record_count": len(valid_records),
        "invalid_record_count": len(records) - len(valid_records),
        "qualification_binding": current,
        "qualification_binding_sha256": qualification_binding_sha256(current),
    }


def record_qualification_evidence(
    root: Path,
    *,
    check: str,
    backend_name: str,
    passed: bool,
    live: bool,
    hashes_verified: bool,
    elapsed_seconds: float,
    worker_session_ids: list[str],
    details: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
    attempt_status: str | None = None,
    run_id: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Append one harness result; non-live/unit evidence is persisted but cannot qualify."""
    if check not in QUALIFICATION_CHECKS:
        raise ValueError(f"unsupported qualification check: {check}")
    if backend_name not in _BACKEND_SPECS:
        raise ValueError(f"unsupported qualification backend: {backend_name}")
    resolved_status = attempt_status or ("passed" if passed else "failed")
    if resolved_status not in {"running", "passed", "failed"}:
        raise ValueError(f"unsupported qualification attempt status: {resolved_status}")
    if resolved_status == "passed" and not passed:
        raise ValueError("a passed qualification attempt must have passed=True")
    runtime = home or runtime_home()
    binding = current_qualification_binding(root, home=runtime)
    evidence_path = qualification_evidence_path(runtime)
    existing = _load_object(evidence_path)
    records = existing.get("records", []) if isinstance(existing, dict) else []
    if not isinstance(records, list):
        records = []
    record = {
        "schema_version": QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id or uuid.uuid4().hex,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "check": check,
        "backend_name": backend_name,
        "backend_kind": "real" if live else "fake",
        "implementation_class": _BACKEND_SPECS[backend_name]["implementation_class"],
        "attempt_status": resolved_status,
        "passed": passed,
        "live": live,
        "hashes_verified": hashes_verified,
        "elapsed_seconds": elapsed_seconds,
        "worker_session_ids": list(worker_session_ids),
        "qualification_binding": binding,
        "qualification_binding_sha256": qualification_binding_sha256(binding),
        "details": dict(details),
        "diagnostics": dict(diagnostics or {}),
    }
    payload = {
        "schema_version": QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
        "updated_at": record["created_at"],
        "qualification_binding": binding,
        "qualification_binding_sha256": qualification_binding_sha256(binding),
        "records": [*records, record][-500:],
    }
    _atomic_json(evidence_path, payload)
    return record


def _files_match(base: Path, entries: object, *, verify_hashes: bool) -> bool:
    if not isinstance(entries, list) or not entries:
        return False
    for raw in entries:
        if not isinstance(raw, dict):
            return False
        relative = raw.get("path")
        size = raw.get("size")
        expected_hash = raw.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            return False
        path = (base / relative).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError:
            return False
        if not path.is_file() or path.stat().st_size != size:
            return False
        if verify_hashes and (
            not isinstance(expected_hash, str) or manifest_hash(path) != expected_hash
        ):
            return False
    return True


def _listed_paths(entries: object) -> set[str] | None:
    if not isinstance(entries, list) or not entries:
        return None
    paths = {
        str(entry.get("path"))
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    return paths if len(paths) == len(entries) else None


def _listed_size_total(entries: object) -> int | None:
    if not isinstance(entries, list) or not entries:
        return None
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None
        total += size
    return total


def _sorted_file_entries(entries: object) -> list[dict[str, Any]] | None:
    if not isinstance(entries, list) or not entries:
        return None
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        path = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
        ):
            return None
        normalized.append({"path": path, "size": size, "sha256": digest})
    return sorted(normalized, key=lambda item: item["path"])


def _runtime_file_inventory(base: Path) -> set[str]:
    """Inventory model artifacts while excluding Hugging Face's disposable cache."""

    if not base.is_dir():
        return set()
    return {
        path.relative_to(base).as_posix()
        for path in base.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(base).parts
    }


def _runtime_versions(python: Path, lock_path: Path) -> dict[str, Any]:
    if not python.is_file() or not lock_path.is_file():
        return {"ok": False, "python": None, "mismatches": ["runtime missing"]}
    wanted: dict[str, str] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line.strip())
        if match:
            wanted[match.group(1)] = match.group(2)
    if not wanted:
        return {"ok": False, "python": None, "mismatches": ["empty runtime lock"]}
    code = textwrap.dedent(
        """
        import importlib.metadata as metadata
        import importlib.util
        import json
        import sys

        wanted = json.loads(sys.argv[1])
        optional_names = json.loads(sys.argv[2])
        got = {}
        mismatches = []
        for name, expected in wanted.items():
            try:
                actual = metadata.version(name)
            except metadata.PackageNotFoundError:
                actual = None
            got[name] = actual
            if actual != expected:
                mismatches.append({"package": name, "expected": expected, "actual": actual})
        normalize = lambda value: value.casefold().replace("_", "-").replace(".", "-")
        installed = {
            normalize(dist.metadata["Name"])
            for dist in metadata.distributions()
            if dist.metadata.get("Name")
        }
        allowed = {normalize(name) for name in wanted} | {"pip"}
        extras = sorted(installed - allowed)
        optional = {
            name: importlib.util.find_spec(name) is not None for name in optional_names
        }
        print(json.dumps({
            "ok": (
                sys.version_info[:2] == (3, 12)
                and not mismatches
                and not extras
                and not any(optional.values())
            ),
            "python": ".".join(map(str, sys.version_info[:3])),
            "packages": got,
            "mismatches": mismatches,
            "unexpected_packages": extras,
            "optional_kernel_modules": optional,
        }))
        """
    )
    try:
        completed = subprocess.run(  # noqa: S603
            [
                str(python),
                "-c",
                code,
                json.dumps(wanted, separators=(",", ":")),
                json.dumps(OPTIONAL_KERNEL_MODULES),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"ok": False, "python": None, "mismatches": ["version probe failed"]}
    return payload if isinstance(payload, dict) else {"ok": False, "mismatches": ["bad probe"]}


def status(root: Path, *, verify_hashes: bool = False) -> dict[str, Any]:
    home = runtime_home()
    model = locked_model(root, "qwythos-airllm")
    measurement_path = home / "qualification.json"
    manifest_path = home / "snapshot-manifest.json"
    binding = current_qualification_binding(root, home=home)
    payload: dict[str, Any] = {
        "home": str(home),
        "python": str(runtime_python(home)),
        "runtime_ready": False,
        "model_id": model.model_id,
        "revision": model.revision,
        "precision": model.precision,
        "gpu_uuid": binding.get("gpu_uuid"),
        "snapshot_ready": manifest_path.is_file(),
        "snapshot_integrity_valid": False,
        "offline_reload_proven": False,
        "native_optional_kernels": False,
        "hashes_verified": verify_hashes,
    }
    payload["manifest"] = _load_object(manifest_path)
    resident_measurement_path = home / "qualification-resident.json"
    evidence_path = qualification_evidence_path(home)
    payload["qualification"] = _load_object(measurement_path)
    payload["resident_qualification"] = _load_object(resident_measurement_path)
    payload["qualification_record_sha256"] = (
        manifest_hash(measurement_path) if measurement_path.is_file() else None
    )
    payload["resident_qualification_record_sha256"] = (
        manifest_hash(resident_measurement_path) if resident_measurement_path.is_file() else None
    )
    payload["qualification_evidence_sha256"] = (
        manifest_hash(evidence_path) if evidence_path.is_file() else None
    )
    payload["qualification_binding"] = binding
    payload["qualification_binding_sha256"] = qualification_binding_sha256(binding)
    shard_manifest_path = home / "shard-manifest.json"
    shards = _load_object(shard_manifest_path)
    payload["shard_manifest"] = (
        {
            "model_id": shards.get("model_id"),
            "revision": shards.get("revision"),
            "source_sha256": shards.get("source_sha256"),
            "source_tensor_count": shards.get("source_tensor_count"),
            "sharded_tensor_count": shards.get("sharded_tensor_count"),
            "excluded_tensor_count": shards.get("excluded_tensor_count"),
            "file_count": len(shards.get("files", [])),
            "manifest_sha256": manifest_hash(shard_manifest_path),
        }
        if isinstance(shards, dict)
        else None
    )
    lock_path = root / "runtimes" / "airllm" / "requirements.lock.txt"
    version_check = _runtime_versions(runtime_python(home), lock_path)
    payload["runtime_versions"] = version_check
    payload["runtime_ready"] = bool(version_check.get("ok"))
    optional_modules = version_check.get("optional_kernel_modules")
    payload["optional_kernel_modules"] = (
        optional_modules if isinstance(optional_modules, dict) else {}
    )
    payload["native_optional_kernels"] = bool(
        isinstance(optional_modules, dict) and any(optional_modules.values())
    )
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        expected_runtime_lock = binding["runtime_lock_sha256"]
        snapshot_root = home / "model"
        locked_snapshot_files = list(locked_qwythos_snapshot_files(root))
        sorted_locked_snapshot_files = _sorted_file_entries(locked_snapshot_files)
        sorted_manifest_snapshot_files = _sorted_file_entries(manifest.get("files"))
        locked_snapshot_paths = _listed_paths(locked_snapshot_files)
        locked_snapshot_bytes = _listed_size_total(locked_snapshot_files)
        listed_snapshot = _listed_paths(manifest.get("files"))
        listed_snapshot_bytes = _listed_size_total(manifest.get("files"))
        snapshot_integrity_valid = bool(
            sorted_manifest_snapshot_files == sorted_locked_snapshot_files
            and locked_snapshot_paths is not None
            and listed_snapshot is not None
            and listed_snapshot == locked_snapshot_paths
            and listed_snapshot == _runtime_file_inventory(snapshot_root)
            and listed_snapshot_bytes == model.expected_snapshot_bytes
            and locked_snapshot_bytes == model.expected_snapshot_bytes
            and _files_match(
                snapshot_root,
                locked_snapshot_files,
                verify_hashes=verify_hashes,
            )
        )
        shard_files_match = isinstance(shards, dict) and _files_match(
            home / "layer-shards" / "splitted_model",
            shards.get("files"),
            verify_hashes=verify_hashes,
        )
        listed_shards = _listed_paths(shards.get("files")) if isinstance(shards, dict) else None
        shard_root = home / "layer-shards" / "splitted_model"
        actual_shards = (
            {
                path.relative_to(shard_root).as_posix()
                for path in shard_root.rglob("*.safetensors")
                if path.is_file()
            }
            if shard_root.is_dir()
            else set()
        )
        actual_done = (
            {
                path.relative_to(shard_root).as_posix().removesuffix(".done")
                for path in shard_root.glob("*.safetensors.done")
                if path.is_file()
            }
            if shard_root.is_dir()
            else set()
        )
        shard_files_match = bool(
            shard_files_match
            and listed_shards is not None
            and actual_shards == listed_shards
            and actual_done == listed_shards
        )
        source_hash = next(
            (
                item.get("sha256")
                for item in manifest.get("files", [])
                if isinstance(item, dict) and item.get("path") == "model.safetensors"
            ),
            None,
        )
        expected_weight_map: dict[str, str] = {}
        shard_files = shards.get("files") if isinstance(shards, dict) else None
        tensor_inventory_valid = isinstance(shard_files, list)
        if isinstance(shard_files, list):
            for entry in shard_files:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("path"), str)
                    or not isinstance(entry.get("tensors"), list)
                    or not all(isinstance(name, str) for name in entry["tensors"])
                    or entry.get("tensor_count") != len(entry["tensors"])
                    or any(name in expected_weight_map for name in entry["tensors"])
                ):
                    tensor_inventory_valid = False
                    break
                expected_weight_map.update(
                    dict.fromkeys(
                        entry["tensors"],
                        f"../layer-shards/splitted_model/{entry['path']}",
                    )
                )
        excluded = shards.get("excluded_tensors", []) if isinstance(shards, dict) else []
        tensor_inventory_valid = bool(
            tensor_inventory_valid
            and isinstance(excluded, list)
            and isinstance(shards, dict)
            and len(expected_weight_map) == shards.get("sharded_tensor_count")
            and len(expected_weight_map) + len(excluded) == shards.get("source_tensor_count")
        )
        shard_manifest_matches = (
            isinstance(shards, dict)
            and shards.get("model_id") == model.model_id
            and shards.get("revision") == model.revision
            and shards.get("source_sha256") == source_hash
            and isinstance(shards.get("sharded_tensor_count"), int)
            and int(shards["sharded_tensor_count"]) > 0
            and all(str(name).startswith("mtp.") for name in shards.get("excluded_tensors", []))
            and tensor_inventory_valid
            and shard_files_match
        )
        resident_index = _load_object(home / "resident-model" / "model.safetensors.index.json")
        resident_ready = (
            isinstance(resident_index, dict)
            and isinstance(resident_index.get("weight_map"), dict)
            and resident_index["weight_map"] == expected_weight_map
        )
        matches_lock = (
            manifest.get("model_id") == model.model_id
            and manifest.get("revision") == model.revision
            and manifest.get("precision") == model.precision
            and manifest.get("actual_snapshot_bytes") == model.expected_snapshot_bytes
            and manifest.get("architecture") == "Qwen3_5ForConditionalGeneration"
            and manifest.get("trust_remote_code") is False
            and manifest.get("runtime_lock_sha256") == expected_runtime_lock
            and snapshot_integrity_valid
            and shard_manifest_matches
            and manifest.get("shard_manifest_sha256")
            == (manifest_hash(shard_manifest_path) if shard_manifest_path.is_file() else None)
        )
        payload["snapshot_integrity_valid"] = snapshot_integrity_valid
        payload["snapshot_ready"] = matches_lock
        payload["shards_ready"] = shard_manifest_matches
        payload["resident_ready"] = resident_ready
        payload["offline_reload_proven"] = matches_lock and bool(
            manifest.get("offline_reload_proven")
        )
    else:
        payload["shards_ready"] = False
        payload["resident_ready"] = False

    payload["qualification_valid"] = valid_qualification_record(
        payload.get("qualification"),
        binding=binding,
        backend_name="airllm",
    )
    payload["resident_qualification_valid"] = valid_qualification_record(
        payload.get("resident_qualification"),
        binding=binding,
        backend_name="transformers-resident",
    )
    airllm_evidence = qualification_evidence_status(
        root,
        backend_name="airllm",
        home=home,
        binding=binding,
    )
    resident_evidence = qualification_evidence_status(
        root,
        backend_name="transformers-resident",
        home=home,
        binding=binding,
    )
    payload["qualification_evidence"] = airllm_evidence
    payload["resident_qualification_evidence"] = resident_evidence
    payload["qualification_evidence_valid"] = airllm_evidence["valid"]
    payload["resident_qualification_evidence_valid"] = resident_evidence["valid"]
    return payload


def setup_plan(root: Path) -> dict[str, Any]:
    home = runtime_home()
    model = locked_model(root, "qwythos-airllm")
    disk = shutil.disk_usage(home.anchor or root.anchor)
    return {
        "home": str(home),
        "free_bytes": disk.free,
        "required_free_bytes": MIN_FREE_BYTES,
        "enough_disk": disk.free >= MIN_FREE_BYTES,
        "python": "3.12",
        "runtime_project": str(root / "runtimes" / "airllm"),
        "model_id": model.model_id,
        "revision": model.revision,
        "expected_snapshot_bytes": model.expected_snapshot_bytes,
        "precision": model.precision,
        "trust_remote_code": model.trust_remote_code,
        "context_limit": model.context_limit,
    }


def runtime_prepared(payload: object) -> bool:
    """Return whether status proves the complete locked runtime is usable."""

    required = (
        "runtime_ready",
        "snapshot_ready",
        "offline_reload_proven",
        "shards_ready",
        "resident_ready",
    )
    return bool(
        isinstance(payload, dict)
        and all(payload.get(field) is True for field in required)
        and payload.get("native_optional_kernels") is False
    )


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)  # noqa: S603


def setup_environment(root: Path) -> dict[str, str]:
    """Build the minimal environment allowed to reach third-party setup code."""
    home = runtime_home()
    env = {name: value for name, value in os.environ.items() if name.upper() in SAFE_ENV_NAMES}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper.startswith("CUDA_") or upper in {
            "NVIDIA_VISIBLE_DEVICES",
            "OMP_NUM_THREADS",
        }:
            env[name] = value
    gpu_uuid = selected_gpu_uuid()
    if gpu_uuid:
        env["CYNTOX_GPU_UUID"] = gpu_uuid
    env.update(
        {
            "CYNTOX_AIRLLM_HOME": str(home),
            "CYNTOX_PROJECT_ROOT": str(root),
            "HF_HOME": str(home / "huggingface"),
            "HF_HUB_CACHE": str(home / "huggingface" / "hub"),
            "PYTHONUTF8": "1",
        }
    )
    # The pinned public checkpoint needs no credential. Never forward ambient account tokens.
    for unsafe in (
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    ):
        env.pop(unsafe, None)
    return env


def _remove_runtime_venv(home: Path, target: Path) -> None:
    resolved_home = home.resolve()
    resolved_target = target.resolve()
    resolved_target.relative_to(resolved_home)
    if not resolved_target.name.startswith(".venv") or resolved_target == resolved_home:
        raise RuntimeError(f"refusing to remove unexpected runtime path: {resolved_target}")
    if resolved_target.exists():
        shutil.rmtree(resolved_target)


def _runtime_child(home: Path, target: Path) -> Path:
    resolved_home = home.resolve()
    resolved_target = target.resolve()
    resolved_target.relative_to(resolved_home)
    if resolved_target == resolved_home:
        raise RuntimeError(f"refusing broad runtime path: {resolved_target}")
    return resolved_target


def _remove_runtime_child(home: Path, target: Path) -> None:
    resolved = _runtime_child(home, target)
    if not resolved.exists():
        return
    if resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        resolved.unlink()


def _quarantine_runtime_child(home: Path, target: Path, *, label: str) -> Path | None:
    resolved = _runtime_child(home, target)
    if not resolved.exists():
        return None
    quarantine = home / f".{label}-quarantine-{uuid.uuid4().hex}"
    os.replace(resolved, quarantine)
    return quarantine


def _restore_quarantine(home: Path, target: Path, quarantine: Path | None) -> None:
    if quarantine is None:
        return
    _remove_runtime_child(home, target)
    os.replace(_runtime_child(home, quarantine), target)


def setup(root: Path) -> dict[str, Any]:
    """Prepare the isolated runtime while excluding admitted AirLLM inference."""
    with ResourceActivityLease(root, "build"):
        return _setup_under_activity(root)


def _setup_under_activity(root: Path) -> dict[str, Any]:
    plan = setup_plan(root)
    if not plan["enough_disk"]:
        raise RuntimeError("AirLLM setup requires at least 50 GiB free disk")
    home = runtime_home()
    home.mkdir(parents=True, exist_ok=True)
    with GpuLease(home / "setup.lease", timeout=3600, heartbeat=5):
        existing = status(root, verify_hashes=True)
        if runtime_prepared(existing):
            return existing
        project = root / "runtimes" / "airllm"
        launcher = sys.executable if sys.version_info[:2] == (3, 12) else None
        launcher_prefix: list[str] = []
        if launcher is None and os.name == "nt":
            launcher = shutil.which("py")
            launcher_prefix = ["-3.12"]
        elif launcher is None:
            launcher = shutil.which("python3.12")
        if not launcher:
            raise RuntimeError("Python 3.12 is required for the AirLLM runtime")
        venv = home / ".venv"
        python = runtime_python(home)
        prepare_env = setup_environment(root)
        backup: Path | None = None
        if python.is_file() and not existing["runtime_ready"]:
            backup = home / f".venv.stale-{uuid.uuid4().hex}"
            os.replace(venv, backup)
        try:
            if not python.is_file():
                create = [launcher, *launcher_prefix, "-m", "venv", str(venv)]
                _run(create, cwd=root)
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "--requirement",
                    str(project / "requirements.lock.txt"),
                ],
                cwd=root,
                env=prepare_env,
            )
            _run(
                [
                    str(python),
                    "-c",
                    (
                        "import torch; "
                        "assert torch.__version__ == '2.14.0+cu130', torch.__version__; "
                        "assert torch.cuda.is_available(), 'CUDA is unavailable'; "
                        "assert torch.cuda.is_bf16_supported(), 'GPU does not support BF16'"
                    ),
                ],
                cwd=root,
                env=prepare_env,
            )
            version_check = _runtime_versions(python, project / "requirements.lock.txt")
            if not version_check.get("ok"):
                raise RuntimeError(f"AirLLM runtime does not match its lock: {version_check}")
        except Exception:
            if backup is not None:
                _remove_runtime_venv(home, venv)
                os.replace(backup, venv)
            raise
        else:
            if backup is not None:
                _remove_runtime_venv(home, backup)
        worker = root / "scripts" / "cyntox_airllm_worker.py"
        with GpuLease(default_gpu_lease_path(), timeout=3600, heartbeat=5):
            ollama = shutil.which("ollama")
            if ollama:
                try:
                    stopped = subprocess.run(  # noqa: S603
                        [ollama, "stop", "cyntox:latest"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                        timeout=30,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    raise RuntimeError("CyntOX unload failed before AirLLM preparation") from error
                if stopped.returncode != 0:
                    raise RuntimeError(
                        "CyntOX unload was not acknowledged before AirLLM preparation"
                    )
            fresh_snapshot = not bool(existing.get("snapshot_integrity_valid"))
            repaired_targets = (
                home / "model",
                home / "snapshot-manifest.json",
                home / "layer-shards",
                home / "shard-manifest.json",
                home / "resident-model",
            )
            quarantined: list[tuple[Path, Path | None]] = []
            if fresh_snapshot:
                for index, target in enumerate(repaired_targets):
                    quarantined.append(
                        (
                            target,
                            _quarantine_runtime_child(
                                home,
                                target,
                                label=f"airllm-{index}",
                            ),
                        )
                    )
            prepare_command = [str(python), str(worker), "prepare", "--root", str(root)]
            if fresh_snapshot:
                prepare_command.append("--fresh-snapshot")
            try:
                _run(
                    prepare_command,
                    cwd=root,
                    env=prepare_env,
                )
            except Exception:
                if fresh_snapshot:
                    for target, _quarantine in reversed(quarantined):
                        _remove_runtime_child(home, target)
                    for target, quarantine in quarantined:
                        _restore_quarantine(home, target, quarantine)
                raise
            else:
                if fresh_snapshot:
                    for _target, quarantine in quarantined:
                        if quarantine is not None:
                            _remove_runtime_child(home, quarantine)
        prepared = status(root, verify_hashes=True)
        if not runtime_prepared(prepared):
            raise RuntimeError("AirLLM setup completed without a fully verified runtime")
        return prepared


def manifest_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def worker_environment(root: Path) -> dict[str, str]:
    home = runtime_home()
    env = {name: value for name, value in os.environ.items() if name.upper() in SAFE_ENV_NAMES}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper.startswith("CUDA_") or upper in {
            "NVIDIA_VISIBLE_DEVICES",
            "OMP_NUM_THREADS",
        }:
            env[name] = value
    gpu_uuid = selected_gpu_uuid()
    if gpu_uuid:
        env["CYNTOX_GPU_UUID"] = gpu_uuid
    env.update(
        {
            "HF_HOME": str(home / "huggingface"),
            "HF_HUB_CACHE": str(home / "huggingface" / "hub"),
            "TRANSFORMERS_CACHE": str(home / "huggingface" / "hub"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "CYNTOX_AIRLLM_HOME": str(home),
            "CYNTOX_PROJECT_ROOT": str(root),
            "HF_DEACTIVATE_ASYNC_LOAD": "1",
            "PYTHONUTF8": "1",
        }
    )
    for unsafe in (
        "CYNTOX_AIRLLM_FAKE",
        "CYNTOX_AIRLLM_FAKE_DELAY",
        "CYNTOX_AIRLLM_FAKE_MODE",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    ):
        env.pop(unsafe, None)
    return env
