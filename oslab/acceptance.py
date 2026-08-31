from __future__ import annotations

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

SELFTEST_COMMAND = ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json"
ACCEPTANCE_AUDIT_COMMAND = ".venv\\Scripts\\python.exe -m oslab.cli acceptance audit --save --json"
EXPECTED_QWEN_CODE_TOOLS = [
    "mcp__oslab__policy_remaining_budget",
    "mcp__oslab__fixture_explain",
]

SELFTEST_EXPECTED_ARGV_TAILS = (
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

ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS = (
    "required_documents_exist",
    "required_artifacts_exist",
    "required_support_files_exist",
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
