from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from oslab.acceptance import (
    ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS,
    EXPECTED_GATE_SUMMARY,
    EXPECTED_QWEN_CODE_TOOLS,
    REQUIRED_ARTIFACTS,
    REQUIRED_DOCS,
    REQUIRED_SUPPORT_FILES,
    audit_acceptance,
)
from oslab.artifacts import ArtifactStore


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git
    result = subprocess.run(  # noqa: S603 - test arguments are fixed locally
        [git, *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")


def _write_artifact_index(root: Path) -> None:
    entries = []
    for relative in (*REQUIRED_DOCS, *REQUIRED_ARTIFACTS, *REQUIRED_SUPPORT_FILES):
        path = root / relative
        if relative == "artifacts/ARTIFACT_INDEX.snapshot.json":
            continue
        data = path.read_bytes()
        entries.append(
            {
                "bytes": len(data),
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    payload = {"generated_at": "2026-08-31T00:00:00Z", "entries": entries}
    index = root / "artifacts" / "ARTIFACT_INDEX.snapshot.json"
    _write(index, json.dumps(payload, indent=2) + "\n")


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _evaluation_rows() -> list[dict[str, object]]:
    variant_names = {
        "A": "vanilla",
        "B": "scaffold",
        "C": "retrieval-memory",
        "D": "adaptive-compute",
        "E": "adversarial-verifier",
    }
    rows: list[dict[str, object]] = []
    for variant, variant_name in variant_names.items():
        for seed in (1, 2, 3):
            rows.append(
                {
                    "baseline_artifacts": {
                        "serial": _sha(f"{variant}-{seed}-baseline"),
                        "stderr": _sha(""),
                    },
                    "baseline_fingerprint": _sha(f"{variant}-{seed}-fingerprint"),
                    "candidate_artifact": _sha(f"{variant}-{seed}-candidate"),
                    "candidate_hash": _sha(f"{variant}-{seed}-candidate-content"),
                    "confidence": 0.95,
                    "evaluator_exploit": 0,
                    "false_positive": 0,
                    "gpu_seconds": 0.1,
                    "infra_failure": 0,
                    "input_tokens": 50,
                    "minimized": 1,
                    "output_tokens": 20,
                    "patch_accepted": 1,
                    "patch_receipt": _sha(f"{variant}-{seed}-patch"),
                    "protocol_states_covered": 2,
                    "regression_outcome": "PASS",
                    "regression_survived": 1,
                    "reproduced": 1,
                    "seed": seed,
                    "seeded_defects_found": 1,
                    "stable_fingerprint": 1,
                    "targeted_outcome": "PASS",
                    "tool_calls": 1,
                    "valid_unique_defects": 1,
                    "variant": variant,
                    "variant_name": variant_name,
                    "verification_artifacts": {
                        "targeted": {
                            "serial": _sha(f"{variant}-{seed}-targeted"),
                            "stderr": _sha(""),
                        },
                        "regression": {
                            "serial": _sha(f"{variant}-{seed}-regression"),
                            "stderr": _sha(""),
                        },
                    },
                    "verifier_artifact": _sha(f"{variant}-{seed}-verifier"),
                    "wall_seconds": 1.0,
                }
            )
    return rows


def _write_required_artifact_payloads(root: Path) -> None:
    model = {
        "architecture": "qwen35",
        "capabilities": ["tools", "thinking", "completion"],
        "context_limit": 262144,
        "endpoint": "http://127.0.0.1:11434",
        "format": "gguf",
        "model_id": "huihui-qwen3.8-27b-abliterated:latest",
        "parameters": 27320697856,
        "provider": "ollama",
        "quantization": "Q4_K_M",
        "runtime": "Ollama",
        "runtime_version": "0.33.2",
    }
    hardware = {
        "schema_version": 1,
        "generated_at": "2026-08-31T00:00:00Z",
        "host": {"os": "Windows", "version": "test", "python": "3.12", "architecture": "AMD64"},
        "cpu": {"model": "test-cpu", "physical_cores": 1, "logical_threads": 2},
        "memory": {"total_bytes": 1024, "available_bytes": 512},
        "gpu": {"present": True, "name": "test-gpu"},
        "disks": [{"device": "C:", "mountpoint": "C:\\", "total_bytes": 1024, "free_bytes": 512}],
        "virtualization": {"accelerators": ["tcg"], "hypervisor_present": True},
        "tooling": {"git": {"present": True}, "ollama": {"present": True}},
        "model_endpoint": {
            "configured": True,
            "loopback_only": True,
            "model_id": model["model_id"],
        },
        "model_files": [],
        "qwen_code": {"present": True, "version": "0.22.3", "project_local": True},
        "target": {"project_role": "orchestrator", "real_os_present": False, "selected": None},
        "safe_disk_benchmark": {"bytes": 1024, "write_mib_s": 1.0, "read_mib_s": 1.0},
    }
    benchmark = {
        "identity": model,
        "average_output_tokens_per_second": 45.0,
        "samples": [
            {
                "seed": seed,
                "completion_tokens": 10,
                "output_tokens_per_second": 40.0 + seed,
                "prompt_tokens": 5,
                "prompt_tokens_per_second": 100.0,
                "wall_seconds": 0.25,
            }
            for seed in (1, 2, 3)
        ],
    }
    runtime_assessment = {
        "generated_at": "2026-08-31T00:00:00Z",
        "installed_commands": {"ollama": True},
        "installed_python_modules": {},
        "selected_runtime": "ollama",
        "selected_model": {
            key: model[key]
            for key in (
                "architecture",
                "context_limit",
                "format",
                "model_id",
                "parameters",
                "quantization",
                "runtime_version",
            )
        },
        "measured": {
            "average_output_tokens_per_second": 45.0,
            "structured_json_smoke": "PASS",
            "qwen_code_mcp_smoke": "PASS",
            "validated_worker_context_tokens": 16384,
        },
        "profiles": {"fast": "enabled", "deep": "enabled", "long": "enabled", "oracle": "disabled"},
    }
    evaluation_rows = _evaluation_rows()
    latest_report = {
        "experiment": "latest",
        "discovery": hardware,
        "benchmark": benchmark,
        "evaluation": {"rows": 15, "variants": ["A", "B", "C", "D", "E"], "accepted": 15},
        "target": {"gate_l": "blocked_missing_external_input"},
        "integrity": {"database": {"ok": True}, "artifacts": {"ok": True}},
    }
    training_rows = []
    for sequence, row in enumerate(evaluation_rows):
        content = {
            "defect_family": "fixture-seeded-calculation",
            "license": "Apache-2.0",
            "patch_accepted": 1,
            "provenance": "artifacts/evaluation/seeded-results.json",
            "regression_survived": 1,
            "seed": row["seed"],
            "variant": row["variant"],
        }
        training_rows.append(
            {
                "schema_version": 1,
                "trajectory_id": f"eval-{row['variant']}-{row['seed']}",
                "sequence": sequence,
                "timestamp": "2026-08-31T00:00:00Z",
                "kind": "evaluation-row",
                "role": str(row["variant_name"]),
                "content": content,
                "content_json": json.dumps(content, sort_keys=True),
                "evidence_hashes": [str(row["candidate_artifact"]), str(row["patch_receipt"])],
                "verified": True,
                "label": "PASS",
                "split": "train",
            }
        )
    negative_content = {
        "defect_family": "evaluator-modification",
        "license": "Apache-2.0",
        "provenance": "deterministic invalid-solution fixture",
    }
    training_rows.append(
        {
            "schema_version": 1,
            "trajectory_id": "fixture-dry-run-negative",
            "sequence": 15,
            "timestamp": "2026-08-31T00:00:00Z",
            "kind": "negative-example",
            "role": "verifier",
            "content": negative_content,
            "content_json": json.dumps(negative_content, sort_keys=True),
            "evidence_hashes": ["f" * 64],
            "verified": True,
            "label": "INVALID_SOLUTION",
            "split": "train",
        }
    )

    _write(root / "artifacts" / "discovery" / "hardware-report.json", json.dumps(hardware) + "\n")
    _write(root / "artifacts" / "discovery" / "model-benchmark.json", json.dumps(benchmark) + "\n")
    _write(
        root / "artifacts" / "discovery" / "runtime-assessment.json",
        json.dumps(runtime_assessment) + "\n",
    )
    _write(
        root / "artifacts" / "evaluation" / "seeded-results.json",
        json.dumps(evaluation_rows) + "\n",
    )
    csv_lines = ["variant,seed"]
    for row in evaluation_rows:
        csv_lines.append(f"{row['variant']},{row['seed']}")
    _write(root / "artifacts" / "evaluation" / "seeded-results.csv", "\n".join(csv_lines) + "\n")
    _write(
        root / "artifacts" / "evaluation" / "EVALUATION_REPORT.md",
        "# Seeded Evaluation Report\n\nSample size: 15 runs.\n\n"
        "| Variant | Acceptance rate |\n"
        "|---|---:|\n"
        "| A — vanilla | 100% |\n"
        "| B — scaffold | 100% |\n"
        "| C — retrieval-memory | 100% |\n"
        "| D — adaptive-compute | 100% |\n"
        "| E — adversarial-verifier | 100% |\n",
    )
    _write(root / "artifacts" / "reports" / "latest-report.json", json.dumps(latest_report) + "\n")
    _write(root / "artifacts" / "reports" / "latest-report.md", "# Latest Report\n")
    _write(
        root / "artifacts" / "training" / "dry-run" / "trajectories.jsonl",
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in training_rows),
    )
    _write(
        root / "artifacts" / "training" / "dry-run" / "DATASET_CARD.md",
        "Records: 16. Source license and evidence-linked provenance are recorded.\n",
    )
    import pyarrow as pa
    import pyarrow.parquet as pq

    parquet_rows = {
        "schema_version": [row["schema_version"] for row in training_rows],
        "trajectory_id": [row["trajectory_id"] for row in training_rows],
        "sequence": [row["sequence"] for row in training_rows],
        "timestamp": [row["timestamp"] for row in training_rows],
        "kind": [row["kind"] for row in training_rows],
        "role": [row["role"] for row in training_rows],
        "label": [row["label"] for row in training_rows],
        "verified": [row["verified"] for row in training_rows],
        "split": [row["split"] for row in training_rows],
        "content_json": [row["content_json"] for row in training_rows],
    }
    parquet_path = root / "artifacts" / "training" / "dry-run" / "trajectories.parquet"
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(parquet_rows), parquet_path)


def _write_selftest_proof_artifact(
    artifact_root: Path, marker: str, *, qwen_result: str = "MCP_BUDGET_OK"
) -> str:
    commands = [
        ["python", "-m", "pytest", "-q"],
        ["python", "-m", "ruff", "format", "--check", "."],
        ["python", "-m", "ruff", "check", "."],
        ["python", "-m", "mypy", "oslab"],
        ["python", "-m", "oslab.cli", "target", "inspect", "--json"],
        ["python", "-m", "oslab.cli", "target", "manifest-template", "--json"],
        ["python", "-m", "oslab.cli", "training", "dry-run", "--json"],
        ["python", "-m", "oslab.cli", "cleanup", "--dry-run", "--json"],
        ["python", "-m", "oslab.cli", "model", "probe", "--live", "--json"],
        ["python", "-m", "oslab.cli", "model", "qwen-code-smoke", "--json"],
        ["python", "-m", "oslab.cli", "integrity", "check", "--json"],
    ]
    rows = []
    for argv in commands:
        stdout = ""
        if argv[-4:] == ["model", "probe", "--live", "--json"]:
            stdout = json.dumps(
                {
                    "identity": {
                        "architecture": "qwen35",
                        "format": "gguf",
                        "model_id": "huihui-qwen3.8-27b-abliterated:latest",
                        "parameters": 27320697856,
                        "quantization": "Q4_K_M",
                        "runtime": "Ollama",
                        "runtime_version": "0.33.2",
                    },
                    "response": {"structured": {"status": "ok", "sum": 4}},
                }
            )
        elif argv[-3:] == ["model", "qwen-code-smoke", "--json"]:
            stdout = json.dumps(
                {
                    "declared_tools": EXPECTED_QWEN_CODE_TOOLS,
                    "tool_calls": ["mcp__oslab__policy_remaining_budget"],
                    "response": {
                        "content": qwen_result,
                        "model": {
                            "model_id": "qwen-os-lab-worker:latest",
                            "runtime": "Qwen Code",
                            "runtime_version": "0.22.3",
                        },
                    },
                }
            )
        rows.append({"argv": argv, "exit_code": 0, "stdout": stdout, "stderr": ""})
    payload = {
        "marker": marker,
        "status": "PASS",
        "commands": rows,
    }
    return ArtifactStore(artifact_root).put_json(payload, "selftest-proof.json").sha256


def _write_acceptance_audit_artifact(artifact_root: Path) -> str:
    payload = {
        "schema_version": 1,
        "status": "PASS",
        "ok": True,
        "failed_checks": [],
        "gate_summary": EXPECTED_GATE_SUMMARY,
        "blocked_gate": {
            "gate": "L",
            "reason": "No authorized OS source path and build entry point are present.",
            "minimal_input": "A local path to the authorized OS source plus its existing build entry point.",
            "resume_command": "oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json",
        },
        "checks": [
            {"name": name, "status": "PASS", "details": {}}
            for name in ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS
        ],
    }
    return ArtifactStore(artifact_root).put_json(payload, "acceptance-gate-audit.json").sha256


def _create_complete_fixture_proof(root: Path) -> None:
    _git(root, "init")
    _write(root / ".gitignore", ".clean/\nartifacts/blobs/\nartifacts/artifact-index.jsonl\n")
    _write(root / "fixtures" / "boot" / "boot.asm", "bits 16\n")
    _write(root / "oslab" / "__init__.py", "__version__ = '0.1.0'\n")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@invalid",
        "commit",
        "-m",
        "verified code",
    )
    source_commit = _git(root, "rev-parse", "HEAD")

    for relative in REQUIRED_DOCS:
        if relative == "PROOF.json":
            continue
        _write(root / relative, f"# {relative}\nverified evidence\n")
    _write_required_artifact_payloads(root)
    for relative in REQUIRED_SUPPORT_FILES:
        _write(root / relative, "schema_version = 1\n")

    clean_worktree = ".clean"
    main_proof_sha = _write_selftest_proof_artifact(root / "artifacts", "main")
    clean_proof_sha = _write_selftest_proof_artifact(root / clean_worktree / "artifacts", "clean")
    audit_sha = _write_acceptance_audit_artifact(root / "artifacts")

    proof = {
        "goal_status": "blocked_on_gate_l",
        "source_commit_verified": source_commit[:7],
        "source_commit_full": source_commit,
        "branch": "codex/qwen-os-lab",
        "blocked_gate": {
            "gate": "L",
            "reason": "No authorized OS source path and build entry point are present.",
            "minimal_input": "A local path to the authorized OS source plus its existing build entry point.",
            "resume_command": "oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json",
        },
        "verified_commands": [
            {
                "scope": "main checkout",
                "command": ".venv\\Scripts\\python.exe -m ruff format --check .",
                "exit_code": 0,
            },
            {
                "scope": "main checkout",
                "command": ".venv\\Scripts\\python.exe -m ruff check .",
                "exit_code": 0,
            },
            {
                "scope": "main checkout",
                "command": ".venv\\Scripts\\python.exe -m mypy oslab",
                "exit_code": 0,
            },
            {
                "scope": "main checkout",
                "command": '.venv\\Scripts\\python.exe -m pytest -m "not live and not qemu" -q',
                "exit_code": 0,
            },
            {
                "scope": "main checkout",
                "command": ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json",
                "exit_code": 0,
                "proof_sha256": main_proof_sha,
            },
            {
                "scope": "clean checkout",
                "command": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\\bootstrap.ps1",
                "exit_code": 0,
                "worktree": clean_worktree,
            },
            {
                "scope": "clean checkout",
                "command": ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json",
                "exit_code": 0,
                "proof_sha256": clean_proof_sha,
            },
            {
                "scope": "main checkout",
                "command": ".venv\\Scripts\\python.exe -m oslab.cli acceptance audit --save --json",
                "exit_code": 0,
                "artifact_sha256": audit_sha,
            },
        ],
        "gate_summary": EXPECTED_GATE_SUMMARY,
        "model": {
            "architecture": "qwen35",
            "format": "gguf",
            "runtime": "Ollama",
            "runtime_version": "0.33.2",
            "model_id": "huihui-qwen3.8-27b-abliterated:latest",
            "parameters": 27320697856,
            "quantization": "Q4_K_M",
        },
        "qwen_code": {
            "version": "0.22.3",
            "wrapper_model": "qwen-os-lab-worker:latest",
            "smoke_result": "MCP_BUDGET_OK",
            "visible_tools": EXPECTED_QWEN_CODE_TOOLS,
        },
    }
    _write(root / "PROOF.json", json.dumps(proof, indent=2) + "\n")
    _write_artifact_index(root)
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@invalid",
        "commit",
        "-m",
        "proof bundle",
    )


def test_acceptance_audit_passes_complete_blocked_gate_l_proof(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)

    result = audit_acceptance(tmp_path)

    assert result["ok"]
    assert not result["failed_checks"]
    assert result["gate_summary"]["L"] == "BLOCKED_MISSING_EXTERNAL_INPUT"


def test_acceptance_audit_rejects_weakened_gate_summary(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["gate_summary"]["N"] = "UNKNOWN"
    _write(proof_path, json.dumps(proof, indent=2) + "\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "gate_summary_matches_contract" in result["failed_checks"]


def test_acceptance_audit_rejects_missing_selftest_proof_artifact(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    for row in proof["verified_commands"]:
        if row.get("scope") == "main checkout" and row.get("command", "").endswith(
            "selftest --live --json"
        ):
            row["proof_sha256"] = "c" * 64
            break
    _write(proof_path, json.dumps(proof, indent=2) + "\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "selftest_proof_artifacts_are_verifiable" in result["failed_checks"]


def test_acceptance_audit_rejects_missing_acceptance_audit_artifact(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    for row in proof["verified_commands"]:
        if row.get("command", "").endswith("acceptance audit --save --json"):
            row["artifact_sha256"] = "d" * 64
            break
    _write(proof_path, json.dumps(proof, indent=2) + "\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "acceptance_audit_artifact_is_verifiable" in result["failed_checks"]


def test_acceptance_audit_rejects_stale_acceptance_audit_artifact(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    stale_payload = {
        "schema_version": 1,
        "status": "PASS",
        "ok": True,
        "failed_checks": [],
        "gate_summary": EXPECTED_GATE_SUMMARY,
        "blocked_gate": proof["blocked_gate"],
        "checks": [
            {"name": name, "status": "PASS", "details": {}}
            for name in ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS
            if name
            not in {
                "required_artifact_contents_are_valid",
                "selftest_live_outputs_match_proof",
            }
        ],
    }
    stale_sha = (
        ArtifactStore(tmp_path / "artifacts")
        .put_json(stale_payload, "acceptance-gate-audit.json")
        .sha256
    )
    for row in proof["verified_commands"]:
        if row.get("command", "").endswith("acceptance audit --save --json"):
            row["artifact_sha256"] = stale_sha
            break
    _write(proof_path, json.dumps(proof, indent=2) + "\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "acceptance_audit_artifact_is_verifiable" in result["failed_checks"]


def test_acceptance_audit_rejects_live_output_that_disagrees_with_proof(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    bad_sha = _write_selftest_proof_artifact(
        tmp_path / "artifacts", "main-mismatch", qwen_result="WRONG_RESULT"
    )
    for row in proof["verified_commands"]:
        if row.get("scope") == "main checkout" and row.get("command", "").endswith(
            "selftest --live --json"
        ):
            row["proof_sha256"] = bad_sha
            break
    _write(proof_path, json.dumps(proof, indent=2) + "\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "selftest_live_outputs_match_proof" in result["failed_checks"]


def test_acceptance_audit_rejects_invalid_required_artifact_content(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    _write(tmp_path / "artifacts" / "evaluation" / "seeded-results.json", "[]\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "required_artifact_contents_are_valid" in result["failed_checks"]
