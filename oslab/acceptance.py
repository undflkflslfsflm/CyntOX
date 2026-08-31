from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from oslab.config import default_config
from oslab.targets import inspect_targets

EXPECTED_GATE_SUMMARY = {
    "A": "PASS",
    "B": "PASS",
    "C": "PASS",
    "D": "PASS",
    "E": "PASS",
    "F": "PASS",
    "G": "PASS",
    "H": "PASS",
    "I": "PASS",
    "J": "PASS",
    "K": "PASS",
    "L": "BLOCKED_MISSING_EXTERNAL_INPUT",
    "M": "PASS",
    "N": "PASS",
    "O": "PASS",
}

REQUIRED_DOCS = (
    "README.md",
    "FINAL_REPORT.md",
    "PROOF.json",
    "docs/ARCHITECTURE.md",
    "docs/THREAT_MODEL.md",
    "docs/RUNBOOK.md",
    "docs/MODEL_RUNTIME_REPORT.md",
    "docs/REAL_OS_INTEGRATION.md",
    "docs/TRAINING_READINESS.md",
    "docs/KNOWN_LIMITATIONS.md",
    "docs/NEXT_EXPERIMENTS.md",
    "docs/HARDWARE_REPORT.md",
    "docs/OSLAB_EXECPLAN.md",
    "docs/OSLAB_PROGRESS.md",
    "docs/OSLAB_DECISIONS.md",
)

REQUIRED_ARTIFACTS = (
    "artifacts/discovery/hardware-report.json",
    "artifacts/discovery/model-benchmark.json",
    "artifacts/discovery/runtime-assessment.json",
    "artifacts/evaluation/seeded-results.json",
    "artifacts/evaluation/seeded-results.csv",
    "artifacts/evaluation/EVALUATION_REPORT.md",
    "artifacts/reports/latest-report.json",
    "artifacts/reports/latest-report.md",
    "artifacts/training/dry-run/trajectories.jsonl",
    "artifacts/training/dry-run/trajectories.parquet",
    "artifacts/training/dry-run/DATASET_CARD.md",
    "artifacts/ARTIFACT_INDEX.snapshot.json",
)

REQUIRED_SUPPORT_FILES = ("config/oslab-target.example.toml",)

REQUIRED_KEY_EVIDENCE_ARTIFACTS = (
    "agentic_fix_loop",
    "supervisor_recovery",
    "fixture_fuzz_campaign",
    "bounded_autonomous_campaign",
    "seeded_evaluation_cas",
    "crash_reproduction",
    "crash_verification",
)

SELFTEST_COMMAND = ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json"
ACCEPTANCE_AUDIT_COMMAND = ".venv\\Scripts\\python.exe -m oslab.cli acceptance audit --save --json"
EXPECTED_QWEN_CODE_TOOLS = [
    "mcp__oslab__policy_remaining_budget",
    "mcp__oslab__fixture_explain",
]

SELFTEST_EXPECTED_ARGV_TAILS = (
    ("lock", "--check"),
    ("install", "--frozen-lockfile", "--offline"),
    ("-m", "pytest", "-q"),
    ("-m", "ruff", "format", "--check", "."),
    ("-m", "ruff", "check", "."),
    ("-m", "mypy", "oslab"),
    ("-m", "oslab.cli", "target", "inspect", "--json"),
    ("-m", "oslab.cli", "target", "manifest-template", "--json"),
    ("-m", "oslab.cli", "training", "dry-run", "--json"),
    ("-m", "oslab.cli", "cleanup", "--dry-run", "--json"),
    ("-m", "oslab.cli", "model", "probe", "--live", "--json"),
    ("-m", "oslab.cli", "model", "qwen-code-smoke", "--json"),
    ("-m", "oslab.cli", "integrity", "check", "--json"),
)
MODEL_PROBE_ARGV_TAIL = ("-m", "oslab.cli", "model", "probe", "--live", "--json")
QWEN_CODE_SMOKE_ARGV_TAIL = ("-m", "oslab.cli", "model", "qwen-code-smoke", "--json")
EXPECTED_EVALUATION_VARIANTS = ("A", "B", "C", "D", "E")
EXPECTED_EVALUATION_SEEDS = (1, 2, 3)
EXPECTED_TRAINING_RECORDS = 16

ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS = (
    "required_documents_exist",
    "required_artifacts_exist",
    "required_support_files_exist",
    "required_artifact_contents_are_valid",
    "key_evidence_artifacts_are_verifiable",
    "gate_summary_matches_contract",
    "gate_l_blocker_is_precise",
    "proof_records_required_commands",
    "proof_records_main_and_clean_selftest_hashes",
    "selftest_proof_artifacts_are_verifiable",
    "selftest_live_outputs_match_proof",
    "proof_records_live_model_identity",
    "proof_records_constrained_qwen_code_smoke",
    "artifact_index_matches_disk",
    "artifact_index_covers_required_files",
    "current_target_inspection_matches_gate_l",
    "post_verified_commit_changes_are_proof_only",
    "git_worktree_is_clean",
    "mandatory_paths_have_no_unresolved_placeholders",
)

PROOF_ONLY_AFTER_VERIFIED_COMMIT_PREFIXES = (
    "artifacts/",
    "docs/",
)

PROOF_ONLY_AFTER_VERIFIED_COMMIT_FILES = {
    "config/oslab-target.example.toml",
    "FINAL_REPORT.md",
    "PROOF.json",
    "README.md",
}

PLACEHOLDER_PATTERNS = (
    "TODO",
    "FIXME",
    "NotImplemented",
    "raise NotImplemented",
    "pytest.mark.skip",
    "skip(",
)

PLACEHOLDER_SCAN_ROOTS = (
    "oslab",
    "tests",
    "fixtures",
    "scripts",
    "docs",
    "README.md",
    "FINAL_REPORT.md",
    "PROOF.json",
)


def audit_acceptance(root: Path) -> dict[str, Any]:
    project_root = root.resolve()
    checks: list[dict[str, Any]] = []

    proof = _load_json(project_root / "PROOF.json")
    _check_required_files(project_root, checks)
    _check_required_artifact_contents(project_root, proof, checks)
    _check_key_evidence_artifacts(project_root, proof, checks)
    _check_gate_summary(proof, checks)
    _check_proof_commands(proof, checks)
    _check_selftest_proof_artifacts(project_root, proof, checks)
    _check_selftest_live_outputs_match_proof(project_root, proof, checks)
    _check_acceptance_audit_artifact(project_root, proof, checks)
    _check_model_and_qwen_code(proof, checks)
    _check_artifact_index(project_root, checks)
    _check_target_gate_l(project_root, checks)
    _check_post_verified_commit_changes(project_root, proof, checks)
    _check_git_worktree_clean(project_root, checks)
    _check_placeholders(project_root, checks)

    failed = [check for check in checks if check["status"] != "PASS"]
    return {
        "schema_version": 1,
        "status": "PASS" if not failed else "FAIL",
        "ok": not failed,
        "checks": checks,
        "failed_checks": [check["name"] for check in failed],
        "gate_summary": proof.get("gate_summary", {}) if isinstance(proof, dict) else {},
        "blocked_gate": proof.get("blocked_gate", {}) if isinstance(proof, dict) else {},
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _load_json_any(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _positive_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _model_identity_mismatches(identity: dict[str, Any], expected_model: object) -> list[str]:
    if not isinstance(expected_model, dict):
        return ["proof_model_missing"]
    mismatches: list[str] = []
    for key in (
        "model_id",
        "architecture",
        "parameters",
        "format",
        "quantization",
        "runtime_version",
    ):
        if identity.get(key) != expected_model.get(key):
            mismatches.append(key)
    if "runtime" in identity and identity.get("runtime") != expected_model.get("runtime"):
        mismatches.append("runtime")
    return mismatches


def _check_required_files(project_root: Path, checks: list[dict[str, Any]]) -> None:
    missing_docs = _missing(project_root, REQUIRED_DOCS)
    missing_artifacts = _missing(project_root, REQUIRED_ARTIFACTS)
    missing_support = _missing(project_root, REQUIRED_SUPPORT_FILES)
    _record(
        checks,
        "required_documents_exist",
        not missing_docs,
        {"missing": missing_docs, "count": len(REQUIRED_DOCS) - len(missing_docs)},
    )
    _record(
        checks,
        "required_artifacts_exist",
        not missing_artifacts,
        {"missing": missing_artifacts, "count": len(REQUIRED_ARTIFACTS) - len(missing_artifacts)},
    )
    _record(
        checks,
        "required_support_files_exist",
        not missing_support,
        {"missing": missing_support, "count": len(REQUIRED_SUPPORT_FILES) - len(missing_support)},
    )


def _missing(project_root: Path, paths: Iterable[str]) -> list[str]:
    return [path for path in paths if not (project_root / path).is_file()]


def _check_required_artifact_contents(
    project_root: Path, proof: dict[str, Any], checks: list[dict[str, Any]]
) -> None:
    failures: list[dict[str, Any]] = []
    _validate_hardware_report(project_root, failures)
    _validate_model_benchmark(project_root, proof, failures)
    _validate_runtime_assessment(project_root, proof, failures)
    _validate_evaluation_outputs(project_root, failures)
    _validate_training_outputs(project_root, failures)
    _validate_latest_report(project_root, failures)
    _record(
        checks,
        "required_artifact_contents_are_valid",
        not failures,
        {"failures": failures},
    )


def _validate_hardware_report(project_root: Path, failures: list[dict[str, Any]]) -> None:
    report = _load_json_any(project_root / "artifacts" / "discovery" / "hardware-report.json")
    path = "artifacts/discovery/hardware-report.json"
    if not isinstance(report, dict):
        failures.append({"path": path, "reason": "not_json_object"})
        return
    required_sections = (
        "host",
        "cpu",
        "memory",
        "gpu",
        "disks",
        "virtualization",
        "tooling",
        "model_endpoint",
        "qwen_code",
        "target",
    )
    missing = [section for section in required_sections if section not in report]
    if missing:
        failures.append({"path": path, "reason": "missing_sections", "sections": missing})
    host = report.get("host")
    cpu = report.get("cpu")
    memory = report.get("memory")
    gpu = report.get("gpu")
    disks = report.get("disks")
    virtualization = report.get("virtualization")
    model_endpoint = report.get("model_endpoint")
    target = report.get("target")
    if not isinstance(host, dict) or not all(host.get(key) for key in ("os", "version", "python")):
        failures.append({"path": path, "reason": "host_identity_incomplete"})
    if not isinstance(cpu, dict) or not _positive_int(cpu.get("logical_threads")):
        failures.append({"path": path, "reason": "cpu_threads_missing"})
    if not isinstance(memory, dict) or not _positive_int(memory.get("total_bytes")):
        failures.append({"path": path, "reason": "memory_total_missing"})
    if not isinstance(gpu, dict) or not isinstance(gpu.get("present"), bool):
        failures.append({"path": path, "reason": "gpu_presence_missing"})
    if not isinstance(disks, list) or not disks:
        failures.append({"path": path, "reason": "disk_inventory_missing"})
    if not isinstance(virtualization, dict) or not isinstance(
        virtualization.get("accelerators"), list
    ):
        failures.append({"path": path, "reason": "virtualization_accelerators_missing"})
    if not isinstance(model_endpoint, dict) or model_endpoint.get("loopback_only") is not True:
        failures.append({"path": path, "reason": "model_endpoint_not_loopback_only"})
    if not isinstance(target, dict) or target.get("real_os_present") is not False:
        failures.append({"path": path, "reason": "target_gate_l_status_not_recorded"})


def _validate_model_benchmark(
    project_root: Path, proof: dict[str, Any], failures: list[dict[str, Any]]
) -> None:
    benchmark = _load_json_any(project_root / "artifacts" / "discovery" / "model-benchmark.json")
    path = "artifacts/discovery/model-benchmark.json"
    if not isinstance(benchmark, dict):
        failures.append({"path": path, "reason": "not_json_object"})
        return
    identity = benchmark.get("identity")
    samples = benchmark.get("samples")
    average = benchmark.get("average_output_tokens_per_second")
    if not isinstance(identity, dict):
        failures.append({"path": path, "reason": "identity_missing"})
    else:
        mismatches = _model_identity_mismatches(identity, proof.get("model", {}))
        if mismatches:
            failures.append({"path": path, "reason": "identity_mismatch", "mismatches": mismatches})
    if not isinstance(average, int | float) or average <= 0:
        failures.append({"path": path, "reason": "average_throughput_missing"})
    if not isinstance(samples, list) or len(samples) < 3:
        failures.append({"path": path, "reason": "benchmark_samples_incomplete"})
        return
    bad_samples = [
        index
        for index, sample in enumerate(samples)
        if not isinstance(sample, dict)
        or not _positive_number(sample.get("output_tokens_per_second"))
        or not _positive_int(sample.get("completion_tokens"))
    ]
    if bad_samples:
        failures.append({"path": path, "reason": "invalid_samples", "samples": bad_samples})


def _validate_runtime_assessment(
    project_root: Path, proof: dict[str, Any], failures: list[dict[str, Any]]
) -> None:
    assessment = _load_json_any(
        project_root / "artifacts" / "discovery" / "runtime-assessment.json"
    )
    path = "artifacts/discovery/runtime-assessment.json"
    if not isinstance(assessment, dict):
        failures.append({"path": path, "reason": "not_json_object"})
        return
    selected_model = assessment.get("selected_model")
    measured = assessment.get("measured")
    profiles = assessment.get("profiles")
    if assessment.get("selected_runtime") != "ollama":
        failures.append({"path": path, "reason": "selected_runtime_not_ollama"})
    if not isinstance(selected_model, dict):
        failures.append({"path": path, "reason": "selected_model_missing"})
    else:
        mismatches = _model_identity_mismatches(selected_model, proof.get("model", {}))
        if mismatches:
            failures.append(
                {"path": path, "reason": "selected_model_mismatch", "mismatches": mismatches}
            )
    if not isinstance(measured, dict) or measured.get("structured_json_smoke") != "PASS":
        failures.append({"path": path, "reason": "structured_json_smoke_not_pass"})
    if not isinstance(measured, dict) or measured.get("qwen_code_mcp_smoke") != "PASS":
        failures.append({"path": path, "reason": "qwen_code_smoke_not_pass"})
    if not isinstance(profiles, dict) or not all(
        profile in profiles for profile in ("fast", "deep", "long", "oracle")
    ):
        failures.append({"path": path, "reason": "runtime_profiles_incomplete"})


def _validate_evaluation_outputs(project_root: Path, failures: list[dict[str, Any]]) -> None:
    rows = _load_json_any(project_root / "artifacts" / "evaluation" / "seeded-results.json")
    path = "artifacts/evaluation/seeded-results.json"
    if not isinstance(rows, list):
        failures.append({"path": path, "reason": "not_json_array"})
        return
    _validate_evaluation_rows(rows, path, failures)
    csv_path = project_root / "artifacts" / "evaluation" / "seeded-results.csv"
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
    except OSError as exc:
        failures.append({"path": "artifacts/evaluation/seeded-results.csv", "reason": str(exc)})
        return
    if len(csv_rows) != len(rows):
        failures.append(
            {
                "path": "artifacts/evaluation/seeded-results.csv",
                "reason": "csv_json_row_count_mismatch",
                "csv": len(csv_rows),
                "json": len(rows),
            }
        )
    if {str(row.get("variant")) for row in csv_rows} != set(EXPECTED_EVALUATION_VARIANTS) or {
        int(row.get("seed", 0)) for row in csv_rows if str(row.get("seed", "")).isdigit()
    } != set(EXPECTED_EVALUATION_SEEDS):
        failures.append(
            {"path": "artifacts/evaluation/seeded-results.csv", "reason": "matrix_incomplete"}
        )
    report_text = _read_text(project_root / "artifacts" / "evaluation" / "EVALUATION_REPORT.md")
    if "Sample size: 15" not in report_text or not all(
        f"| {variant} " in report_text for variant in EXPECTED_EVALUATION_VARIANTS
    ):
        failures.append(
            {
                "path": "artifacts/evaluation/EVALUATION_REPORT.md",
                "reason": "summary_missing_matrix",
            }
        )


def _validate_evaluation_rows(rows: list[Any], path: str, failures: list[dict[str, Any]]) -> None:
    if len(rows) != len(EXPECTED_EVALUATION_VARIANTS) * len(EXPECTED_EVALUATION_SEEDS):
        failures.append({"path": path, "reason": "matrix_row_count_wrong", "rows": len(rows)})
    variants = {row.get("variant") for row in rows if isinstance(row, dict)}
    seeds = {row.get("seed") for row in rows if isinstance(row, dict)}
    if variants != set(EXPECTED_EVALUATION_VARIANTS) or seeds != set(EXPECTED_EVALUATION_SEEDS):
        failures.append({"path": path, "reason": "matrix_variants_or_seeds_incomplete"})
    bad_rows: list[int] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            bad_rows.append(index)
            continue
        if (
            row.get("patch_accepted") != 1
            or row.get("reproduced") != 1
            or row.get("stable_fingerprint") != 1
            or row.get("regression_survived") != 1
            or row.get("false_positive") != 0
            or row.get("infra_failure") != 0
            or row.get("evaluator_exploit") != 0
            or row.get("targeted_outcome") != "PASS"
            or row.get("regression_outcome") != "PASS"
            or not _is_sha256(row.get("candidate_artifact"))
            or not _is_sha256(row.get("patch_receipt"))
        ):
            bad_rows.append(index)
    if bad_rows:
        failures.append({"path": path, "reason": "invalid_evaluation_rows", "rows": bad_rows})


def _validate_training_outputs(project_root: Path, failures: list[dict[str, Any]]) -> None:
    jsonl_path = project_root / "artifacts" / "training" / "dry-run" / "trajectories.jsonl"
    path = "artifacts/training/dry-run/trajectories.jsonl"
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(_read_text(jsonl_path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            failures.append({"path": path, "reason": "jsonl_decode_error", "line": index})
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            failures.append({"path": path, "reason": "jsonl_row_not_object", "line": index})
    if len(rows) != EXPECTED_TRAINING_RECORDS:
        failures.append({"path": path, "reason": "record_count_wrong", "rows": len(rows)})
    if len({row.get("trajectory_id") for row in rows}) != len(rows):
        failures.append({"path": path, "reason": "trajectory_ids_not_unique"})
    labels = {row.get("label") for row in rows}
    if labels != {"PASS", "INVALID_SOLUTION"}:
        failures.append(
            {"path": path, "reason": "labels_incomplete", "labels": sorted(map(str, labels))}
        )
    bad_rows = [
        index
        for index, row in enumerate(rows)
        if row.get("schema_version") != 1
        or row.get("verified") is not True
        or not isinstance(row.get("content_json"), str)
        or not all(_is_sha256(value) for value in row.get("evidence_hashes", []))
    ]
    if bad_rows:
        failures.append({"path": path, "reason": "invalid_training_rows", "rows": bad_rows})
    parquet_path = project_root / "artifacts" / "training" / "dry-run" / "trajectories.parquet"
    try:
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(parquet_path)
    except Exception as exc:
        failures.append(
            {"path": "artifacts/training/dry-run/trajectories.parquet", "reason": str(exc)}
        )
    else:
        required_columns = {"schema_version", "trajectory_id", "label", "verified", "content_json"}
        if parquet.metadata.num_rows != len(rows) or not required_columns.issubset(
            set(parquet.schema.names)
        ):
            failures.append(
                {
                    "path": "artifacts/training/dry-run/trajectories.parquet",
                    "reason": "parquet_schema_or_row_count_wrong",
                }
            )
    card_text = _read_text(project_root / "artifacts" / "training" / "dry-run" / "DATASET_CARD.md")
    card_lower = card_text.lower()
    if (
        "records: 16" not in card_lower
        or "license" not in card_lower
        or "evidence-linked" not in card_lower
    ):
        failures.append(
            {
                "path": "artifacts/training/dry-run/DATASET_CARD.md",
                "reason": "card_missing_record_or_provenance_summary",
            }
        )


def _validate_latest_report(project_root: Path, failures: list[dict[str, Any]]) -> None:
    report = _load_json_any(project_root / "artifacts" / "reports" / "latest-report.json")
    path = "artifacts/reports/latest-report.json"
    if not isinstance(report, dict):
        failures.append({"path": path, "reason": "not_json_object"})
        return
    evaluation = report.get("evaluation")
    target = report.get("target")
    integrity = report.get("integrity")
    if (
        not isinstance(evaluation, dict)
        or evaluation.get("rows") != 15
        or evaluation.get("accepted") != 15
    ):
        failures.append({"path": path, "reason": "evaluation_summary_invalid"})
    if not isinstance(evaluation, dict) or evaluation.get("variants") != list(
        EXPECTED_EVALUATION_VARIANTS
    ):
        failures.append({"path": path, "reason": "evaluation_variants_invalid"})
    if not isinstance(target, dict) or target.get("gate_l") != "blocked_missing_external_input":
        failures.append({"path": path, "reason": "target_gate_l_summary_invalid"})
    if (
        not isinstance(integrity, dict)
        or not isinstance(integrity.get("database"), dict)
        or integrity["database"].get("ok") is not True
        or not isinstance(integrity.get("artifacts"), dict)
        or integrity["artifacts"].get("ok") is not True
    ):
        failures.append({"path": path, "reason": "integrity_summary_invalid"})


def _check_key_evidence_artifacts(
    project_root: Path, proof: dict[str, Any], checks: list[dict[str, Any]]
) -> None:
    evidence = proof.get("key_evidence_artifacts")
    failures: list[dict[str, Any]] = []
    if not isinstance(evidence, dict):
        _record(
            checks,
            "key_evidence_artifacts_are_verifiable",
            False,
            {"failures": [{"key": "<root>", "reason": "missing_key_evidence_artifacts"}]},
        )
        return
    missing = [key for key in REQUIRED_KEY_EVIDENCE_ARTIFACTS if not _is_sha256(evidence.get(key))]
    for key in missing:
        failures.append({"key": key, "reason": "missing_or_invalid_sha256"})
    for key in REQUIRED_KEY_EVIDENCE_ARTIFACTS:
        digest = evidence.get(key)
        if not _is_sha256(digest):
            continue
        assert isinstance(digest, str)
        payload = _load_artifact_payload(project_root / "artifacts", digest)
        if not payload["ok"]:
            failures.append({"key": key, "sha256": digest, "reason": payload["reason"]})
            continue
        parsed = payload["json"]
        if key == "agentic_fix_loop":
            _validate_agentic_fix_artifact(project_root, parsed, key, failures, proof)
        elif key == "supervisor_recovery":
            _validate_recovery_artifact(parsed, key, failures)
        elif key == "fixture_fuzz_campaign":
            _validate_fuzz_campaign_artifact(project_root, parsed, key, failures)
        elif key == "bounded_autonomous_campaign":
            _validate_bounded_campaign_artifact(project_root, parsed, key, failures)
        elif key == "seeded_evaluation_cas":
            if not isinstance(parsed, list):
                failures.append({"key": key, "reason": "evaluation_payload_not_array"})
            else:
                _validate_evaluation_rows(parsed, key, failures)
        elif key == "crash_reproduction":
            _validate_crash_reproduction_artifact(project_root, parsed, key, failures)
        elif key == "crash_verification":
            _validate_crash_verification_artifact(project_root, parsed, key, failures)
    _record(
        checks,
        "key_evidence_artifacts_are_verifiable",
        not failures,
        {"failures": failures},
    )


def _validate_agentic_fix_artifact(
    project_root: Path,
    payload: Any,
    key: str,
    failures: list[dict[str, Any]],
    proof: dict[str, Any],
) -> None:
    if not isinstance(payload, dict):
        failures.append({"key": key, "reason": "payload_not_object"})
        return
    targeted = payload.get("targeted")
    regressions = payload.get("regressions")
    verifier = payload.get("verifier")
    invalid_verifier = payload.get("invalid_verifier")
    proposal = payload.get("proposal")
    diff = str(payload.get("diff", ""))
    if not payload.get("run_id"):
        failures.append({"key": key, "reason": "run_id_missing"})
    if "fixtures/boot/boot.asm" not in diff or "mov al, '4'" not in diff:
        failures.append({"key": key, "reason": "fixture_patch_diff_missing"})
    if not isinstance(targeted, dict) or targeted.get("outcome") != "PASS":
        failures.append({"key": key, "reason": "targeted_fix_not_pass"})
    elif "OSLAB_BUG_VALUE 4" not in str(targeted.get("serial_log", "")):
        failures.append({"key": key, "reason": "targeted_serial_missing_fixed_value"})
    else:
        _validate_artifact_reference_dict(project_root, targeted.get("artifacts"), key, failures)
        details = targeted.get("details")
        if not isinstance(details, dict) or details.get("network") != "none":
            failures.append({"key": key, "reason": "targeted_network_not_disabled"})
    if not isinstance(regressions, list) or len(regressions) < 2:
        failures.append({"key": key, "reason": "regressions_incomplete"})
    else:
        modes = {
            regression.get("details", {}).get("mode")
            for regression in regressions
            if isinstance(regression, dict) and isinstance(regression.get("details"), dict)
        }
        if not {"pass", "snapshot"}.issubset(modes):
            failures.append({"key": key, "reason": "required_regression_modes_missing"})
        for regression in regressions:
            if not isinstance(regression, dict) or regression.get("outcome") != "PASS":
                failures.append({"key": key, "reason": "regression_not_pass"})
                continue
            _validate_artifact_reference_dict(
                project_root, regression.get("artifacts"), key, failures
            )
            details = regression.get("details")
            if not isinstance(details, dict) or details.get("network") != "none":
                failures.append({"key": key, "reason": "regression_network_not_disabled"})
    if _structured_verdict(verifier) != "accept":
        failures.append({"key": key, "reason": "valid_patch_not_accepted_by_verifier"})
    if _structured_verdict(invalid_verifier) != "reject":
        failures.append({"key": key, "reason": "invalid_solution_not_rejected"})
    proposal_model = proposal.get("model") if isinstance(proposal, dict) else None
    if isinstance(proposal_model, dict):
        mismatches = _model_identity_mismatches(proposal_model, proof.get("model", {}))
        if mismatches:
            failures.append(
                {"key": key, "reason": "proposal_model_mismatch", "mismatches": mismatches}
            )
    else:
        failures.append({"key": key, "reason": "proposal_model_missing"})


def _validate_recovery_artifact(payload: Any, key: str, failures: list[dict[str, Any]]) -> None:
    if not isinstance(payload, dict):
        failures.append({"key": key, "reason": "payload_not_object"})
        return
    integrity = payload.get("integrity")
    if (
        payload.get("controlled_exit_code") != 97
        or payload.get("interrupted_state") != "GENERATE_TEST"
        or payload.get("final_state") != "COMPLETE"
        or payload.get("finding_count") != 1
        or not _positive_int(payload.get("transition_count"))
        or not isinstance(payload.get("crash_command"), list)
        or not isinstance(payload.get("resume_command"), list)
        or not payload.get("run_id")
    ):
        failures.append({"key": key, "reason": "recovery_summary_invalid"})
    if not isinstance(integrity, dict) or integrity.get("ok") is not True:
        failures.append({"key": key, "reason": "recovery_integrity_not_ok"})


def _validate_fuzz_campaign_artifact(
    project_root: Path, payload: Any, key: str, failures: list[dict[str, Any]]
) -> None:
    if not isinstance(payload, dict):
        failures.append({"key": key, "reason": "payload_not_object"})
        return
    coverage = payload.get("coverage")
    observations = payload.get("observations")
    unique_findings = payload.get("unique_findings")
    minimized = payload.get("minimized")
    if not isinstance(coverage, dict) or coverage.get("complete") is not True:
        failures.append({"key": key, "reason": "coverage_not_complete"})
    else:
        required_modes = {"pass", "fail", "crash", "hang", "seeded", "induced-infra"}
        if not required_modes.issubset(set(coverage.get("modes", []))):
            failures.append({"key": key, "reason": "coverage_modes_incomplete"})
        if not {"PASS", "FAIL", "CRASH", "HANG", "INFRA_ERROR"}.issubset(
            set(coverage.get("outcomes", []))
        ):
            failures.append({"key": key, "reason": "coverage_outcomes_incomplete"})
    iterations = payload.get("iterations")
    if not (isinstance(iterations, int) and not isinstance(iterations, bool) and iterations >= 6):
        failures.append({"key": key, "reason": "iterations_insufficient"})
    unique_inputs = payload.get("unique_inputs")
    if not (
        isinstance(unique_inputs, int)
        and not isinstance(unique_inputs, bool)
        and unique_inputs >= 6
    ):
        failures.append({"key": key, "reason": "unique_inputs_insufficient"})
    if not isinstance(observations, list) or len(observations) < 6:
        failures.append({"key": key, "reason": "observations_incomplete"})
    else:
        modes = {row.get("mode") for row in observations if isinstance(row, dict)}
        outcomes = {row.get("outcome") for row in observations if isinstance(row, dict)}
        if not {"pass", "fail", "crash", "hang", "seeded", "induced-infra"}.issubset(modes):
            failures.append({"key": key, "reason": "observation_modes_incomplete"})
        if not {"PASS", "FAIL", "CRASH", "HANG", "INFRA_ERROR"}.issubset(outcomes):
            failures.append({"key": key, "reason": "observation_outcomes_incomplete"})
        for observation in observations:
            if isinstance(observation, dict):
                _validate_artifact_reference_dict(
                    project_root, observation.get("artifacts"), key, failures
                )
    if not isinstance(unique_findings, dict) or len(unique_findings) < 4:
        failures.append({"key": key, "reason": "unique_findings_incomplete"})
    if not isinstance(minimized, dict) or minimized.get("replay_outcome") != "CRASH":
        failures.append({"key": key, "reason": "minimized_crash_not_replayed"})
    else:
        original_bytes = minimized.get("original_bytes")
        minimized_bytes = minimized.get("minimized_bytes")
        if (
            not isinstance(original_bytes, int)
            or isinstance(original_bytes, bool)
            or not isinstance(minimized_bytes, int)
            or isinstance(minimized_bytes, bool)
            or original_bytes <= 0
            or minimized_bytes <= 0
        ):
            failures.append({"key": key, "reason": "minimized_size_invalid"})
        elif minimized_bytes > original_bytes:
            failures.append({"key": key, "reason": "minimized_larger_than_original"})


def _validate_bounded_campaign_artifact(
    project_root: Path, payload: Any, key: str, failures: list[dict[str, Any]]
) -> None:
    if not isinstance(payload, dict):
        failures.append({"key": key, "reason": "payload_not_object"})
        return
    if (
        payload.get("target") != "fixture"
        or payload.get("hypotheses_generated") is not True
        or payload.get("tests_executed") is not True
        or payload.get("errors_handled") is not True
        or payload.get("findings_deduplicated") is not True
        or not payload.get("campaign_id")
    ):
        failures.append({"key": key, "reason": "bounded_campaign_summary_invalid"})
    recovery = payload.get("recovery")
    if not isinstance(recovery, dict) or recovery.get("final_state") != "COMPLETE":
        failures.append({"key": key, "reason": "bounded_campaign_recovery_invalid"})
    fuzz = payload.get("fuzz")
    if not isinstance(fuzz, dict):
        failures.append({"key": key, "reason": "bounded_campaign_fuzz_missing"})
    else:
        _validate_fuzz_campaign_artifact(project_root, fuzz, key, failures)


def _validate_crash_reproduction_artifact(
    project_root: Path, payload: Any, key: str, failures: list[dict[str, Any]]
) -> None:
    if not isinstance(payload, dict):
        failures.append({"key": key, "reason": "payload_not_object"})
        return
    cold_boots = payload.get("cold_boots")
    if (
        not (isinstance(cold_boots, int) and not isinstance(cold_boots, bool) and cold_boots >= 2)
        or payload.get("stable") is not True
        or payload.get("mode") != "crash"
    ):
        failures.append({"key": key, "reason": "crash_reproduction_summary_invalid"})
    outcomes = payload.get("outcomes")
    fingerprints = payload.get("fingerprints")
    expected = payload.get("expected_fingerprint")
    fingerprint_values = (
        [fingerprint for fingerprint in fingerprints if isinstance(fingerprint, str)]
        if isinstance(fingerprints, list)
        else []
    )
    if (
        not isinstance(outcomes, list)
        or len(outcomes) < 2
        or any(outcome != "CRASH" for outcome in outcomes)
    ):
        failures.append({"key": key, "reason": "crash_outcomes_invalid"})
    if (
        not isinstance(fingerprints, list)
        or len(fingerprint_values) != len(fingerprints)
        or len(fingerprint_values) < 2
        or len(set(fingerprint_values)) != 1
        or not _is_sha256(expected)
        or fingerprint_values[0] != expected
    ):
        failures.append({"key": key, "reason": "crash_fingerprint_not_stable"})
    _validate_artifact_reference_list(project_root, payload.get("artifacts"), key, failures)


def _validate_crash_verification_artifact(
    project_root: Path, payload: Any, key: str, failures: list[dict[str, Any]]
) -> None:
    if not isinstance(payload, dict):
        failures.append({"key": key, "reason": "payload_not_object"})
        return
    if payload.get("accepted") is not True or payload.get("expected") != "CRASH":
        failures.append({"key": key, "reason": "crash_verification_not_accepted"})
    cold_boots = payload.get("cold_boots")
    if (
        not (isinstance(cold_boots, int) and not isinstance(cold_boots, bool) and cold_boots >= 2)
        or payload.get("stable") is not True
        or payload.get("mode") != "crash"
    ):
        failures.append({"key": key, "reason": "crash_verification_summary_invalid"})
    outcomes = payload.get("outcomes")
    fingerprints = payload.get("fingerprints")
    fingerprint_values = (
        [fingerprint for fingerprint in fingerprints if isinstance(fingerprint, str)]
        if isinstance(fingerprints, list)
        else []
    )
    if (
        not isinstance(outcomes, list)
        or len(outcomes) < 2
        or any(outcome != "CRASH" for outcome in outcomes)
    ):
        failures.append({"key": key, "reason": "crash_verification_outcomes_invalid"})
    if (
        not isinstance(fingerprints, list)
        or len(fingerprint_values) != len(fingerprints)
        or len(fingerprint_values) < 2
        or len(set(fingerprint_values)) != 1
        or not _is_sha256(fingerprint_values[0])
    ):
        failures.append({"key": key, "reason": "crash_verification_fingerprint_not_stable"})
    _validate_artifact_reference_list(project_root, payload.get("artifacts"), key, failures)


def _validate_artifact_reference_list(
    project_root: Path, value: Any, key: str, failures: list[dict[str, Any]]
) -> None:
    if not isinstance(value, list) or not value:
        failures.append({"key": key, "reason": "artifact_reference_list_missing"})
        return
    for item in value:
        _validate_artifact_reference_dict(project_root, item, key, failures)


def _validate_artifact_reference_dict(
    project_root: Path, value: Any, key: str, failures: list[dict[str, Any]]
) -> None:
    if not isinstance(value, dict):
        failures.append({"key": key, "reason": "artifact_reference_missing"})
        return
    for name in ("serial", "stderr"):
        digest = value.get(name)
        if not _is_sha256(digest):
            failures.append({"key": key, "reason": f"{name}_artifact_hash_invalid"})
            continue
        assert isinstance(digest, str)
        if not _artifact_blob_exists(project_root / "artifacts", digest):
            failures.append({"key": key, "reason": f"{name}_artifact_missing", "sha256": digest})


def _artifact_blob_exists(artifact_root: Path, digest: str) -> bool:
    path = artifact_root / "blobs" / "sha256" / digest[:2] / digest
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest


def _structured_verdict(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    structured = value.get("structured")
    if isinstance(structured, dict):
        verdict = structured.get("verdict")
        return str(verdict) if verdict is not None else None
    return None


def _check_gate_summary(proof: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    gate_summary = proof.get("gate_summary", {})
    mismatches = {
        gate: {"expected": expected, "actual": gate_summary.get(gate)}
        for gate, expected in EXPECTED_GATE_SUMMARY.items()
        if not isinstance(gate_summary, dict) or gate_summary.get(gate) != expected
    }
    _record(checks, "gate_summary_matches_contract", not mismatches, {"mismatches": mismatches})

    blocked_gate = proof.get("blocked_gate", {})
    reason = str(blocked_gate.get("reason", "")) if isinstance(blocked_gate, dict) else ""
    gate_l_precise = (
        isinstance(blocked_gate, dict)
        and blocked_gate.get("gate") == "L"
        and "authorized" in reason
        and "OS source" in reason
        and "AUTHORIZED_OS_SOURCE_PATH" in str(blocked_gate.get("resume_command", ""))
    )
    _record(checks, "gate_l_blocker_is_precise", gate_l_precise, {"blocked_gate": blocked_gate})


def _check_proof_commands(proof: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    commands = proof.get("verified_commands", [])
    command_rows = [row for row in commands if isinstance(row, dict)]

    def matching(scope: str, command: str) -> list[dict[str, Any]]:
        return [
            row
            for row in command_rows
            if row.get("scope") == scope and row.get("command") == command
        ]

    required = {
        "main_quality_format": (
            "main checkout",
            ".venv\\Scripts\\python.exe -m ruff format --check .",
        ),
        "main_quality_lint": ("main checkout", ".venv\\Scripts\\python.exe -m ruff check ."),
        "main_quality_typecheck": (
            "main checkout",
            ".venv\\Scripts\\python.exe -m mypy oslab",
        ),
        "main_quality_pytest_subset": (
            "main checkout",
            '.venv\\Scripts\\python.exe -m pytest -m "not live and not qemu" -q',
        ),
        "main_live_selftest": (
            "main checkout",
            SELFTEST_COMMAND,
        ),
        "clean_bootstrap": (
            "clean checkout",
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\\bootstrap.ps1",
        ),
        "clean_live_selftest": (
            "clean checkout",
            SELFTEST_COMMAND,
        ),
    }
    missing_or_failed: dict[str, Any] = {}
    for name, (scope, command) in required.items():
        rows = matching(scope, command)
        if not rows:
            missing_or_failed[name] = "missing"
            continue
        if not any(row.get("exit_code") == 0 for row in rows):
            missing_or_failed[name] = "nonzero"
    _record(
        checks,
        "proof_records_required_commands",
        not missing_or_failed,
        {"missing_or_failed": missing_or_failed},
    )

    selftest_hashes = [
        row.get("proof_sha256") for row in command_rows if row.get("command") == SELFTEST_COMMAND
    ]
    valid_hashes = [value for value in selftest_hashes if _is_sha256(value)]
    _record(
        checks,
        "proof_records_main_and_clean_selftest_hashes",
        len(valid_hashes) >= 2,
        {"hashes": valid_hashes},
    )


def _check_selftest_proof_artifacts(
    project_root: Path, proof: dict[str, Any], checks: list[dict[str, Any]]
) -> None:
    command_rows = [row for row in proof.get("verified_commands", []) if isinstance(row, dict)]
    clean_worktree = _clean_worktree_path(project_root, command_rows)
    selftest_rows = [
        row
        for row in command_rows
        if row.get("command") == SELFTEST_COMMAND and row.get("exit_code") == 0
    ]
    failures: list[dict[str, Any]] = []
    for row in selftest_rows:
        scope = str(row.get("scope", "<unknown>"))
        digest = row.get("proof_sha256")
        if not _is_sha256(digest):
            failures.append({"scope": scope, "reason": "invalid_or_missing_proof_sha256"})
            continue
        assert isinstance(digest, str)
        artifact_root = project_root / "artifacts"
        if scope == "clean checkout":
            if clean_worktree is None:
                failures.append({"scope": scope, "sha256": digest, "reason": "missing_worktree"})
                continue
            artifact_root = clean_worktree / "artifacts"
        payload = _load_artifact_payload(artifact_root, digest)
        if not payload["ok"]:
            failures.append({"scope": scope, "sha256": digest, "reason": payload["reason"]})
            continue
        proof_payload = payload["json"]
        if not isinstance(proof_payload, dict) or proof_payload.get("status") != "PASS":
            failures.append({"scope": scope, "sha256": digest, "reason": "selftest_not_pass"})
            continue
        commands = proof_payload.get("commands", [])
        if not isinstance(commands, list) or not commands:
            failures.append({"scope": scope, "sha256": digest, "reason": "missing_commands"})
            continue
        failed_commands = [
            _argv_for_details(command)
            for command in commands
            if not isinstance(command, dict) or command.get("exit_code") != 0
        ]
        missing_tails = [
            " ".join(tail)
            for tail in SELFTEST_EXPECTED_ARGV_TAILS
            if not any(
                isinstance(command, dict) and _argv_endswith(command.get("argv"), tail)
                for command in commands
            )
        ]
        if failed_commands or missing_tails:
            failures.append(
                {
                    "scope": scope,
                    "sha256": digest,
                    "reason": "selftest_commands_incomplete_or_failed",
                    "failed_commands": failed_commands,
                    "missing": missing_tails,
                }
            )
    _record(
        checks,
        "selftest_proof_artifacts_are_verifiable",
        len(selftest_rows) >= 2 and not failures,
        {"checked": len(selftest_rows), "failures": failures},
    )


def _check_acceptance_audit_artifact(
    project_root: Path, proof: dict[str, Any], checks: list[dict[str, Any]]
) -> None:
    command_rows = [row for row in proof.get("verified_commands", []) if isinstance(row, dict)]
    audit_rows = [
        row
        for row in command_rows
        if row.get("command") == ACCEPTANCE_AUDIT_COMMAND and row.get("exit_code") == 0
    ]
    failures: list[str] = []
    if not audit_rows:
        failures.append("missing_acceptance_audit_command")
    for row in audit_rows:
        digest = row.get("artifact_sha256")
        if not _is_sha256(digest):
            failures.append("invalid_or_missing_artifact_sha256")
            continue
        assert isinstance(digest, str)
        payload = _load_artifact_payload(project_root / "artifacts", digest)
        if not payload["ok"]:
            failures.append(str(payload["reason"]))
            continue
        audit_payload = payload["json"]
        if not isinstance(audit_payload, dict):
            failures.append("audit_artifact_not_object")
            continue
        if audit_payload.get("status") != "PASS" or audit_payload.get("ok") is not True:
            failures.append("audit_artifact_not_pass")
        if audit_payload.get("failed_checks") != []:
            failures.append("audit_artifact_has_failed_checks")
        if audit_payload.get("gate_summary") != proof.get("gate_summary"):
            failures.append("audit_artifact_gate_summary_mismatch")
        if audit_payload.get("blocked_gate") != proof.get("blocked_gate"):
            failures.append("audit_artifact_blocked_gate_mismatch")
        checks_by_name = {
            check.get("name"): check.get("status")
            for check in audit_payload.get("checks", [])
            if isinstance(check, dict)
        }
        missing = [
            name
            for name in ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS
            if checks_by_name.get(name) != "PASS"
        ]
        if missing:
            failures.append("audit_artifact_missing_passed_checks")
    _record(
        checks,
        "acceptance_audit_artifact_is_verifiable",
        bool(audit_rows) and not failures,
        {"checked": len(audit_rows), "failures": failures},
    )


def _check_selftest_live_outputs_match_proof(
    project_root: Path, proof: dict[str, Any], checks: list[dict[str, Any]]
) -> None:
    model = proof.get("model", {})
    qwen_code = proof.get("qwen_code", {})
    command_rows = [row for row in proof.get("verified_commands", []) if isinstance(row, dict)]
    clean_worktree = _clean_worktree_path(project_root, command_rows)
    selftest_rows = [
        row
        for row in command_rows
        if row.get("command") == SELFTEST_COMMAND and row.get("exit_code") == 0
    ]
    failures: list[dict[str, Any]] = []
    for row in selftest_rows:
        scope = str(row.get("scope", "<unknown>"))
        digest = row.get("proof_sha256")
        if not _is_sha256(digest):
            failures.append({"scope": scope, "reason": "invalid_or_missing_proof_sha256"})
            continue
        assert isinstance(digest, str)
        artifact_root = project_root / "artifacts"
        if scope == "clean checkout":
            if clean_worktree is None:
                failures.append({"scope": scope, "reason": "missing_worktree"})
                continue
            artifact_root = clean_worktree / "artifacts"
        payload = _load_artifact_payload(artifact_root, digest)
        if not payload["ok"]:
            failures.append({"scope": scope, "reason": str(payload["reason"])})
            continue
        commands = payload["json"].get("commands", []) if isinstance(payload["json"], dict) else []
        if not isinstance(commands, list):
            failures.append({"scope": scope, "reason": "missing_commands"})
            continue
        model_probe = _command_by_tail(commands, MODEL_PROBE_ARGV_TAIL)
        qwen_smoke = _command_by_tail(commands, QWEN_CODE_SMOKE_ARGV_TAIL)
        if model_probe is None:
            failures.append({"scope": scope, "reason": "missing_model_probe_output"})
        else:
            model_failures = _model_probe_mismatches(model_probe, model)
            if model_failures:
                failures.append(
                    {
                        "scope": scope,
                        "reason": "model_probe_output_mismatch",
                        "mismatches": model_failures,
                    }
                )
        if qwen_smoke is None:
            failures.append({"scope": scope, "reason": "missing_qwen_code_smoke_output"})
        else:
            qwen_failures = _qwen_code_smoke_mismatches(qwen_smoke, qwen_code)
            if qwen_failures:
                failures.append(
                    {
                        "scope": scope,
                        "reason": "qwen_code_smoke_output_mismatch",
                        "mismatches": qwen_failures,
                    }
                )
    _record(
        checks,
        "selftest_live_outputs_match_proof",
        len(selftest_rows) >= 2 and not failures,
        {"checked": len(selftest_rows), "failures": failures},
    )


def _clean_worktree_path(project_root: Path, command_rows: list[dict[str, Any]]) -> Path | None:
    for row in command_rows:
        worktree = row.get("worktree")
        if (
            row.get("scope") == "clean checkout"
            and row.get("command")
            == "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\\bootstrap.ps1"
            and row.get("exit_code") == 0
            and isinstance(worktree, str)
        ):
            return (project_root / worktree).resolve()
    return None


def _load_artifact_payload(artifact_root: Path, digest: str) -> dict[str, Any]:
    path = artifact_root / "blobs" / "sha256" / digest[:2] / digest
    if not path.is_file():
        return {"ok": False, "reason": "missing_artifact_blob"}
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        return {"ok": False, "reason": "artifact_hash_mismatch"}
    try:
        text = data.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "reason": "artifact_is_not_json"}
    return {"ok": True, "json": parsed}


def _argv_endswith(value: object, tail: tuple[str, ...]) -> bool:
    if not isinstance(value, list) or len(value) < len(tail):
        return False
    argv = [str(item) for item in value]
    return tuple(argv[-len(tail) :]) == tail


def _argv_for_details(command: object) -> str:
    if not isinstance(command, dict):
        return "<non-object-command>"
    argv = command.get("argv")
    if not isinstance(argv, list):
        return "<missing-argv>"
    return " ".join(str(part) for part in argv)


def _command_by_tail(commands: list[Any], tail: tuple[str, ...]) -> dict[str, Any] | None:
    for command in commands:
        if isinstance(command, dict) and _argv_endswith(command.get("argv"), tail):
            return command
    return None


def _json_stdout(command: dict[str, Any]) -> dict[str, Any]:
    stdout = command.get("stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        return {}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _model_probe_mismatches(command: dict[str, Any], model: object) -> list[str]:
    if not isinstance(model, dict):
        return ["proof_model_missing"]
    payload = _json_stdout(command)
    if not payload:
        return ["model_probe_stdout_not_json"]
    identity = payload.get("identity")
    response = payload.get("response")
    if not isinstance(identity, dict) or not isinstance(response, dict):
        return ["model_probe_missing_identity_or_response"]
    mismatches: list[str] = []
    for key in (
        "runtime",
        "runtime_version",
        "model_id",
        "architecture",
        "parameters",
        "format",
        "quantization",
    ):
        if model.get(key) != identity.get(key):
            mismatches.append(f"identity.{key}")
    structured = response.get("structured")
    if (
        not isinstance(structured, dict)
        or structured.get("status") != "ok"
        or structured.get("sum") != 4
    ):
        mismatches.append("response.structured")
    return mismatches


def _qwen_code_smoke_mismatches(command: dict[str, Any], qwen_code: object) -> list[str]:
    if not isinstance(qwen_code, dict):
        return ["proof_qwen_code_missing"]
    payload = _json_stdout(command)
    if not payload:
        return ["qwen_code_stdout_not_json"]
    response = payload.get("response")
    response_model = response.get("model") if isinstance(response, dict) else None
    mismatches: list[str] = []
    if payload.get("declared_tools") != EXPECTED_QWEN_CODE_TOOLS:
        mismatches.append("declared_tools")
    if payload.get("tool_calls") != ["mcp__oslab__policy_remaining_budget"]:
        mismatches.append("tool_calls")
    if not isinstance(response, dict) or response.get("content") != qwen_code.get("smoke_result"):
        mismatches.append("response.content")
    if not isinstance(response_model, dict):
        mismatches.append("response.model")
    else:
        if response_model.get("runtime") != "Qwen Code":
            mismatches.append("response.model.runtime")
        if response_model.get("runtime_version") != qwen_code.get("version"):
            mismatches.append("response.model.runtime_version")
        if response_model.get("model_id") != qwen_code.get("wrapper_model"):
            mismatches.append("response.model.model_id")
    if qwen_code.get("visible_tools") != EXPECTED_QWEN_CODE_TOOLS:
        mismatches.append("proof.visible_tools")
    return mismatches


def _check_model_and_qwen_code(proof: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    model = proof.get("model", {})
    qwen_code = proof.get("qwen_code", {})
    model_ok = (
        isinstance(model, dict)
        and model.get("runtime") == "Ollama"
        and isinstance(model.get("model_id"), str)
        and model.get("parameters")
        and model.get("quantization")
    )
    _record(checks, "proof_records_live_model_identity", bool(model_ok), {"model": model})

    qwen_code_ok = (
        isinstance(qwen_code, dict)
        and qwen_code.get("smoke_result") == "MCP_BUDGET_OK"
        and qwen_code.get("visible_tools") == EXPECTED_QWEN_CODE_TOOLS
    )
    _record(
        checks,
        "proof_records_constrained_qwen_code_smoke",
        bool(qwen_code_ok),
        {"qwen_code": qwen_code},
    )


def _check_artifact_index(project_root: Path, checks: list[dict[str, Any]]) -> None:
    index_path = project_root / "artifacts" / "ARTIFACT_INDEX.snapshot.json"
    index = _load_json(index_path)
    rows = index.get("entries", [])
    failures: list[dict[str, Any]] = []
    indexed_paths: set[str] = set()
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                failures.append(
                    {"path": "<non-object-entry>", "reason": "index entry is not an object"}
                )
                continue
            logical = str(row.get("path", ""))
            indexed_paths.add(logical)
            target = project_root / logical
            if not target.is_file():
                failures.append({"path": logical, "reason": "missing"})
                continue
            actual_hash = _sha256_file(target)
            actual_bytes = target.stat().st_size
            if row.get("sha256") != actual_hash or row.get("bytes") != actual_bytes:
                failures.append(
                    {
                        "path": logical,
                        "reason": "hash_or_size_mismatch",
                        "expected_sha256": row.get("sha256"),
                        "actual_sha256": actual_hash,
                        "expected_bytes": row.get("bytes"),
                        "actual_bytes": actual_bytes,
                    }
                )
    else:
        failures.append({"path": str(index_path), "reason": "entries is not a list"})

    missing_index_entries = [
        path
        for path in (*REQUIRED_DOCS, *REQUIRED_ARTIFACTS, *REQUIRED_SUPPORT_FILES)
        if path != "artifacts/ARTIFACT_INDEX.snapshot.json" and path not in indexed_paths
    ]
    _record(
        checks,
        "artifact_index_matches_disk",
        not failures,
        {"failures": failures},
    )
    _record(
        checks,
        "artifact_index_covers_required_files",
        not missing_index_entries,
        {"missing": missing_index_entries},
    )


def _check_target_gate_l(project_root: Path, checks: list[dict[str, Any]]) -> None:
    result = inspect_targets(default_config(project_root))
    target_ok = (
        result.get("fixture", {}).get("status") == "ready"
        and result.get("gate_l") == "blocked_missing_external_input"
        and result.get("real_os", {}).get("status") == "absent"
        and "AUTHORIZED_OS_SOURCE_PATH" in str(result.get("resume_command", ""))
    )
    _record(checks, "current_target_inspection_matches_gate_l", target_ok, result)


def _check_post_verified_commit_changes(
    project_root: Path, proof: dict[str, Any], checks: list[dict[str, Any]]
) -> None:
    source_commit = str(
        proof.get("source_commit_full") or proof.get("source_commit_verified") or ""
    )
    if not source_commit:
        _record(
            checks,
            "post_verified_commit_changes_are_proof_only",
            False,
            {"reason": "missing source commit"},
        )
        return
    git = shutil.which("git")
    if git is None:
        _record(
            checks,
            "post_verified_commit_changes_are_proof_only",
            False,
            {"reason": "git not found"},
        )
        return
    completed = subprocess.run(  # noqa: S603 - resolved git binary, fixed command shape
        [git, "diff", "--name-only", f"{source_commit}..HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        _record(
            checks,
            "post_verified_commit_changes_are_proof_only",
            False,
            {"reason": completed.stderr.strip() or completed.stdout.strip()},
        )
        return
    changed = [
        line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()
    ]
    disallowed = [path for path in changed if not _is_proof_only_path(path)]
    _record(
        checks,
        "post_verified_commit_changes_are_proof_only",
        not disallowed,
        {"changed_paths": changed, "disallowed": disallowed, "source_commit": source_commit},
    )


def _check_git_worktree_clean(project_root: Path, checks: list[dict[str, Any]]) -> None:
    git = shutil.which("git")
    if git is None:
        _record(checks, "git_worktree_is_clean", False, {"reason": "git not found"})
        return
    completed = subprocess.run(  # noqa: S603 - resolved git binary, fixed command shape
        [git, "status", "--short"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        _record(
            checks,
            "git_worktree_is_clean",
            False,
            {"reason": completed.stderr.strip() or completed.stdout.strip()},
        )
        return
    status_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    _record(checks, "git_worktree_is_clean", not status_lines, {"status": status_lines})


def _is_proof_only_path(path: str) -> bool:
    return path in PROOF_ONLY_AFTER_VERIFIED_COMMIT_FILES or path.startswith(
        PROOF_ONLY_AFTER_VERIFIED_COMMIT_PREFIXES
    )


def _check_placeholders(project_root: Path, checks: list[dict[str, Any]]) -> None:
    hits: list[dict[str, Any]] = []
    for relative_root in PLACEHOLDER_SCAN_ROOTS:
        target = project_root / relative_root
        if target.is_file():
            _scan_file_for_placeholders(project_root, target, hits)
        elif target.is_dir():
            for path in target.rglob("*"):
                if path.is_file() and path.suffix.lower() in {
                    ".py",
                    ".md",
                    ".ps1",
                    ".sh",
                    ".asm",
                    ".json",
                }:
                    _scan_file_for_placeholders(project_root, path, hits)
    _record(checks, "mandatory_paths_have_no_unresolved_placeholders", not hits, {"hits": hits})


def _scan_file_for_placeholders(project_root: Path, path: Path, hits: list[dict[str, Any]]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    in_own_pattern_declaration = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if path.name == "acceptance.py" and line.startswith("PLACEHOLDER_PATTERNS = ("):
            in_own_pattern_declaration = True
            continue
        if in_own_pattern_declaration:
            if line == ")":
                in_own_pattern_declaration = False
            continue
        for pattern in PLACEHOLDER_PATTERNS:
            if pattern in line:
                hits.append(
                    {
                        "path": path.relative_to(project_root).as_posix(),
                        "line": line_number,
                        "pattern": pattern,
                    }
                )


def _record(checks: list[dict[str, Any]], name: str, passed: bool, details: dict[str, Any]) -> None:
    checks.append({"name": name, "status": "PASS" if passed else "FAIL", "details": details})


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
