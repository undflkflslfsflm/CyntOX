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

PROOF_ONLY_AFTER_VERIFIED_COMMIT_PREFIXES = (
    "artifacts/",
    "docs/",
)

PROOF_ONLY_AFTER_VERIFIED_COMMIT_FILES = {
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
            ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json",
        ),
        "clean_bootstrap": (
            "clean checkout",
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\\bootstrap.ps1",
        ),
        "clean_live_selftest": (
            "clean checkout",
            ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json",
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
        row.get("proof_sha256")
        for row in command_rows
        if row.get("command") == ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json"
    ]
    valid_hashes = [value for value in selftest_hashes if _is_sha256(value)]
    _record(
        checks,
        "proof_records_main_and_clean_selftest_hashes",
        len(valid_hashes) >= 2,
        {"hashes": valid_hashes},
    )


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
        and qwen_code.get("visible_tools")
        == ["mcp__oslab__policy_remaining_budget", "mcp__oslab__fixture_explain"]
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
        for path in (*REQUIRED_DOCS, *REQUIRED_ARTIFACTS)
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
