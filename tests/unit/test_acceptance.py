from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from oslab import acceptance
from oslab.acceptance import (
    ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS,
    EXPECTED_GATE_SUMMARY,
    EXPECTED_QWEN_CODE_TOOLS,
    GATE_L_BLOCKER_REPORT_PATH,
    REQUIRED_ARTIFACTS,
    REQUIRED_DOCS,
    REQUIRED_KEY_EVIDENCE_ARTIFACTS,
    REQUIRED_SUPPORT_FILES,
    REQUIREMENTS_TRACE_PATH,
    audit_acceptance,
    build_gate_l_blocker_report,
    build_requirements_trace,
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


def test_placeholder_scan_allows_qemu_availability_skip(tmp_path: Path) -> None:
    conftest = tmp_path / "tests" / "conftest.py"
    skip_marker = "pytest.mark." + "skip"
    _write(
        conftest,
        f'skip = {skip_marker}(reason="QEMU e2e tests require reachable Docker daemon")\n',
    )
    hits: list[dict[str, object]] = []

    acceptance._scan_file_for_placeholders(tmp_path, conftest, hits)

    assert hits == []


def test_placeholder_scan_rejects_unrelated_skip(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_unfinished.py"
    skip_marker = "pytest.mark." + "skip"
    _write(test_file, f'{skip_marker}(reason="not implemented yet")\n')
    hits: list[dict[str, object]] = []

    acceptance._scan_file_for_placeholders(tmp_path, test_file, hits)

    assert hits == [
        {
            "path": "tests/test_unfinished.py",
            "line": 1,
            "pattern": "pytest.mark." + "skip",
        },
    ]


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


def _artifact_pair(store: ArtifactStore, label: str) -> dict[str, str]:
    serial = store.put_bytes(
        f"{label} serial evidence".encode(),
        f"{label}-serial.log",
        "text/plain",
    ).sha256
    stderr = store.put_bytes(
        f"{label} stderr evidence".encode(),
        f"{label}-stderr.log",
        "text/plain",
    ).sha256
    return {"serial": serial, "stderr": stderr}


def _test_model_identity() -> dict[str, object]:
    return {
        "architecture": "qwen35",
        "format": "gguf",
        "runtime": "Ollama",
        "runtime_version": "0.33.2",
        "model_id": "huihui-qwen3.8-27b-abliterated:latest",
        "parameters": 27320697856,
        "quantization": "Q4_K_M",
    }


def _test_blocked_gate() -> dict[str, object]:
    return {
        "gate": "L",
        "reason": "No authorized OS source path and build entry point are present.",
        "minimal_input": "A local path to the authorized OS source plus its existing build entry point.",
        "resume_command": "oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json",
    }


def _fuzz_campaign_payload(store: ArtifactStore, label: str) -> dict[str, object]:
    cases = [
        ("pass", "PASS"),
        ("fail", "FAIL"),
        ("crash", "CRASH"),
        ("hang", "HANG"),
        ("seeded", "CRASH"),
        ("induced-infra", "INFRA_ERROR"),
    ]
    observations = [
        {
            "case_id": f"{label}-{index}",
            "mode": mode,
            "outcome": outcome,
            "artifacts": _artifact_pair(store, f"{label}-{mode}"),
        }
        for index, (mode, outcome) in enumerate(cases, start=1)
    ]
    return {
        "coverage": {
            "complete": True,
            "modes": [mode for mode, _ in cases],
            "outcomes": ["PASS", "FAIL", "CRASH", "HANG", "INFRA_ERROR"],
        },
        "iterations": 6,
        "unique_inputs": 6,
        "observations": observations,
        "unique_findings": {
            "fail": _sha(f"{label}-finding-fail"),
            "crash": _sha(f"{label}-finding-crash"),
            "hang": _sha(f"{label}-finding-hang"),
            "infra": _sha(f"{label}-finding-infra"),
        },
        "minimized": {
            "replay_outcome": "CRASH",
            "original_bytes": 32,
            "minimized_bytes": 8,
        },
    }


def _crash_reproduction_payload(store: ArtifactStore, label: str) -> dict[str, object]:
    fingerprint = _sha(f"{label}-fingerprint")
    return {
        "cold_boots": 3,
        "stable": True,
        "mode": "crash",
        "outcomes": ["CRASH", "CRASH", "CRASH"],
        "fingerprints": [fingerprint, fingerprint, fingerprint],
        "expected_fingerprint": fingerprint,
        "artifacts": [
            _artifact_pair(store, f"{label}-cold-boot-1"),
            _artifact_pair(store, f"{label}-cold-boot-2"),
            _artifact_pair(store, f"{label}-cold-boot-3"),
        ],
    }


def _write_key_evidence_artifacts(root: Path) -> dict[str, str]:
    store = ArtifactStore(root / "artifacts")
    fixture_fuzz = _fuzz_campaign_payload(store, "fixture-fuzz")
    bounded_fuzz = _fuzz_campaign_payload(store, "bounded-fuzz")
    crash_reproduction = _crash_reproduction_payload(store, "crash-reproduction")
    crash_verification = {
        **_crash_reproduction_payload(store, "crash-verification"),
        "accepted": True,
        "expected": "CRASH",
    }
    crash_verification.pop("expected_fingerprint")
    payloads: dict[str, object] = {
        "agentic_fix_loop": {
            "run_id": "agentic-fix-loop-test",
            "diff": (
                "diff --git a/fixtures/boot/boot.asm b/fixtures/boot/boot.asm\n"
                "-    mov al, '3'\n"
                "+    mov al, '4'\n"
            ),
            "targeted": {
                "outcome": "PASS",
                "serial_log": "READY\nOSLAB_BUG_VALUE 4\nPASS\n",
                "details": {"mode": "seeded", "network": "none"},
                "artifacts": _artifact_pair(store, "agentic-targeted"),
            },
            "regressions": [
                {
                    "outcome": "PASS",
                    "details": {"mode": "pass", "network": "none"},
                    "artifacts": _artifact_pair(store, "agentic-regression-pass"),
                },
                {
                    "outcome": "PASS",
                    "details": {"mode": "snapshot", "network": "none"},
                    "artifacts": _artifact_pair(store, "agentic-regression-snapshot"),
                },
            ],
            "verifier": {"structured": {"verdict": "accept"}},
            "invalid_verifier": {"structured": {"verdict": "reject"}},
            "proposal": {"model": _test_model_identity()},
        },
        "supervisor_recovery": {
            "run_id": "supervisor-recovery-test",
            "controlled_exit_code": 97,
            "interrupted_state": "GENERATE_TEST",
            "final_state": "COMPLETE",
            "finding_count": 1,
            "transition_count": 12,
            "crash_command": [
                "python",
                "-m",
                "oslab.cli",
                "campaign",
                "--crash-after",
                "GENERATE_TEST",
            ],
            "resume_command": ["python", "-m", "oslab.cli", "campaign", "--resume"],
            "integrity": {"ok": True},
        },
        "fixture_fuzz_campaign": fixture_fuzz,
        "bounded_autonomous_campaign": {
            "target": "fixture",
            "campaign_id": "bounded-campaign-test",
            "hypotheses_generated": True,
            "tests_executed": True,
            "errors_handled": True,
            "findings_deduplicated": True,
            "recovery": {"final_state": "COMPLETE"},
            "fuzz": bounded_fuzz,
        },
        "seeded_evaluation_cas": _evaluation_rows(),
        "crash_reproduction": crash_reproduction,
        "crash_verification": crash_verification,
    }
    assert set(payloads) == set(REQUIRED_KEY_EVIDENCE_ARTIFACTS)
    return {key: store.put_json(payload, f"{key}.json").sha256 for key, payload in payloads.items()}


def _write_required_artifact_payloads(
    root: Path,
    *,
    source_commit: str,
    main_selftest_sha: str,
    clean_selftest_sha: str,
    blocked_gate: dict[str, object],
) -> None:
    model = _test_model_identity() | {
        "capabilities": ["tools", "thinking", "completion"],
        "context_limit": 262144,
        "endpoint": "http://127.0.0.1:11434",
        "provider": "ollama",
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
    gate_l_blocker_report = {
        "schema_version": 1,
        "gate": "L",
        "status": "BLOCKED_MISSING_EXTERNAL_INPUT",
        "proof_blocked_gate": blocked_gate,
        "minimal_input": blocked_gate["minimal_input"],
        "what_was_attempted": [
            "Bounded target discovery inspected the allowed local scope.",
            "Main checkout live selftest ran target inspect.",
            "Clean checkout live selftest ran target inspect.",
            "Manifest-backed build, boot, and test paths were implemented.",
        ],
        "concrete_evidence": {
            "source_commit_full": source_commit,
            "main_selftest_proof_sha256": main_selftest_sha,
            "clean_selftest_proof_sha256": clean_selftest_sha,
            "target_inspect": {
                "fixture_status": "ready",
                "gate_l": "blocked_missing_external_input",
                "real_os_status": "absent",
                "bounded_candidates": 0,
            },
        },
        "why_further_progress_is_impossible": [
            "Gate L requires a real authorized OS source tree.",
            "The existing build entry point is not locally present in the allowed scope.",
            "The lab must not invent build commands or search unrelated personal files.",
        ],
        "resume_commands": [
            ".\\.venv\\Scripts\\python.exe -m oslab.cli target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json",
            ".\\.venv\\Scripts\\python.exe -m oslab.cli target manifest-template --json",
            ".\\.venv\\Scripts\\python.exe -m oslab.cli target validate-manifest --repo <AUTHORIZED_OS_SOURCE_PATH> --json",
            ".\\.venv\\Scripts\\python.exe -m oslab.cli build --target real --repo <AUTHORIZED_OS_SOURCE_PATH> --profile debug --json",
            ".\\.venv\\Scripts\\python.exe -m oslab.cli boot --target real --repo <AUTHORIZED_OS_SOURCE_PATH> --profile debug --json",
            ".\\.venv\\Scripts\\python.exe -m oslab.cli test --target real --repo <AUTHORIZED_OS_SOURCE_PATH> --test smoke --profile debug --json",
        ],
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
        root / GATE_L_BLOCKER_REPORT_PATH,
        json.dumps(gate_l_blocker_report, indent=2, sort_keys=True) + "\n",
    )
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
        ["uv", "lock", "--check"],
        ["pnpm", "install", "--frozen-lockfile", "--offline"],
        ["python", "-m", "pytest", "-q"],
        ["python", "-m", "ruff", "format", "--check", "."],
        ["python", "-m", "ruff", "check", "."],
        ["python", "-m", "mypy", "oslab", "scripts"],
        ["python", "-m", "oslab.cli", "target", "inspect", "--json"],
        ["python", "-m", "oslab.cli", "target", "manifest-template", "--json"],
        ["python", "-m", "oslab.cli", "target", "blocker-report", "--json"],
        ["python", "-m", "oslab.cli", "acceptance", "trace", "--json"],
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
        "blocked_gate": _test_blocked_gate(),
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
    clean_worktree = ".clean"
    _git(root, "worktree", "add", "--detach", clean_worktree, source_commit)

    for relative in REQUIRED_DOCS:
        if relative == "PROOF.json":
            continue
        _write(root / relative, f"# {relative}\nverified evidence\n")
    for relative in REQUIRED_SUPPORT_FILES:
        _write(root / relative, "schema_version = 1\n")

    main_proof_sha = _write_selftest_proof_artifact(root / "artifacts", "main")
    clean_proof_sha = _write_selftest_proof_artifact(root / clean_worktree / "artifacts", "clean")
    audit_sha = _write_acceptance_audit_artifact(root / "artifacts")
    key_evidence_artifacts = _write_key_evidence_artifacts(root)
    blocked_gate = _test_blocked_gate()
    _write_required_artifact_payloads(
        root,
        source_commit=source_commit,
        main_selftest_sha=main_proof_sha,
        clean_selftest_sha=clean_proof_sha,
        blocked_gate=blocked_gate,
    )

    proof = {
        "goal_status": "blocked_on_gate_l",
        "source_commit_verified": source_commit[:7],
        "source_commit_full": source_commit,
        "branch": "codex/qwen-os-lab",
        "blocked_gate": blocked_gate,
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
        "key_artifacts": {"requirements_trace": REQUIREMENTS_TRACE_PATH},
        "model": _test_model_identity(),
        "qwen_code": {
            "version": "0.22.3",
            "wrapper_model": "qwen-os-lab-worker:latest",
            "smoke_result": "MCP_BUDGET_OK",
            "visible_tools": EXPECTED_QWEN_CODE_TOOLS,
        },
        "key_evidence_artifacts": key_evidence_artifacts,
    }
    _write(root / "PROOF.json", json.dumps(proof, indent=2) + "\n")
    _write(
        root / GATE_L_BLOCKER_REPORT_PATH,
        json.dumps(build_gate_l_blocker_report(root), indent=2) + "\n",
    )
    _write(
        root / REQUIREMENTS_TRACE_PATH,
        json.dumps(build_requirements_trace(root), indent=2) + "\n",
    )
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


def test_acceptance_audit_rejects_invalid_gate_l_blocker_report(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    _write(
        tmp_path / GATE_L_BLOCKER_REPORT_PATH,
        json.dumps(
            {
                "schema_version": 1,
                "gate": "L",
                "status": "PASS",
                "minimal_input": "something vague",
                "resume_commands": [],
            },
            indent=2,
        )
        + "\n",
    )
    _write_artifact_index(tmp_path)

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "gate_l_blocker_report_is_verifiable" in result["failed_checks"]


def test_acceptance_audit_rejects_hand_edited_gate_l_blocker_report(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    report = build_gate_l_blocker_report(tmp_path)
    report["what_was_attempted"][0] = "A different but still non-empty attempted-work claim."
    _write(tmp_path / GATE_L_BLOCKER_REPORT_PATH, json.dumps(report, indent=2) + "\n")
    _write_artifact_index(tmp_path)

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "gate_l_blocker_report_is_verifiable" in result["failed_checks"]


def test_acceptance_audit_rejects_invalid_requirements_trace(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    trace = build_requirements_trace(tmp_path)
    for row in trace["trace"]:
        if row["gate"] == "L":
            row["status"] = "PASS"
            row["minimal_input_needed"] = "anything"
            break
    _write(tmp_path / REQUIREMENTS_TRACE_PATH, json.dumps(trace, indent=2) + "\n")
    _write_artifact_index(tmp_path)

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "requirements_trace_is_verifiable" in result["failed_checks"]


def test_acceptance_audit_rejects_hand_edited_requirements_trace(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    trace = build_requirements_trace(tmp_path)
    trace["trace"][0]["requirement"] = "A different but still non-empty Gate A claim."
    _write(tmp_path / REQUIREMENTS_TRACE_PATH, json.dumps(trace, indent=2) + "\n")
    _write_artifact_index(tmp_path)

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "requirements_trace_is_verifiable" in result["failed_checks"]


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


def test_acceptance_audit_rejects_selftest_without_dependency_checks(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    stale_commands = [
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
    stale_payload = {
        "status": "PASS",
        "commands": [
            {"argv": command, "exit_code": 0, "stdout": "", "stderr": ""}
            for command in stale_commands
        ],
    }
    stale_sha = (
        ArtifactStore(tmp_path / "artifacts").put_json(stale_payload, "selftest-proof.json").sha256
    )
    for row in proof["verified_commands"]:
        if row.get("scope") == "main checkout" and row.get("command", "").endswith(
            "selftest --live --json"
        ):
            row["proof_sha256"] = stale_sha
            break
    _write(proof_path, json.dumps(proof, indent=2) + "\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "selftest_proof_artifacts_are_verifiable" in result["failed_checks"]


def test_acceptance_audit_rejects_clean_checkout_at_wrong_commit(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    _write(tmp_path / ".clean" / "drift.txt", "new tracked content\n")
    _git(tmp_path / ".clean", "add", "drift.txt")
    _git(
        tmp_path / ".clean",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@invalid",
        "commit",
        "-m",
        "move clean checkout",
    )

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "clean_checkout_matches_verified_source" in result["failed_checks"]


def test_acceptance_audit_rejects_dirty_clean_checkout(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    _write(tmp_path / ".clean" / "dirty.txt", "uncommitted clean-checkout drift\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "clean_checkout_matches_verified_source" in result["failed_checks"]


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


def test_acceptance_audit_rejects_audit_artifact_without_key_evidence_check(
    tmp_path: Path,
) -> None:
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
            if name != "key_evidence_artifacts_are_verifiable"
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


def test_acceptance_audit_rejects_audit_artifact_without_clean_checkout_check(
    tmp_path: Path,
) -> None:
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
            if name != "clean_checkout_matches_verified_source"
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


def test_acceptance_audit_rejects_audit_artifact_with_mismatched_gate_summary(
    tmp_path: Path,
) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    mismatched_gate_summary = dict(EXPECTED_GATE_SUMMARY)
    mismatched_gate_summary["L"] = "PASS"
    stale_payload = {
        "schema_version": 1,
        "status": "PASS",
        "ok": True,
        "failed_checks": [],
        "gate_summary": mismatched_gate_summary,
        "blocked_gate": proof["blocked_gate"],
        "checks": [
            {"name": name, "status": "PASS", "details": {}}
            for name in ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS
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


def test_acceptance_audit_rejects_audit_artifact_with_mismatched_blocker(
    tmp_path: Path,
) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    stale_payload = {
        "schema_version": 1,
        "status": "PASS",
        "ok": True,
        "failed_checks": [],
        "gate_summary": EXPECTED_GATE_SUMMARY,
        "blocked_gate": {
            **proof["blocked_gate"],
            "minimal_input": "anything at all",
        },
        "checks": [
            {"name": name, "status": "PASS", "details": {}}
            for name in ACCEPTANCE_ARTIFACT_REQUIRED_CHECKS
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


def test_acceptance_audit_rejects_invalid_key_evidence_artifact(tmp_path: Path) -> None:
    _create_complete_fixture_proof(tmp_path)
    proof_path = tmp_path / "PROOF.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["key_evidence_artifacts"]["agentic_fix_loop"] = "e" * 64
    _write(proof_path, json.dumps(proof, indent=2) + "\n")

    result = audit_acceptance(tmp_path)

    assert not result["ok"]
    assert "key_evidence_artifacts_are_verifiable" in result["failed_checks"]
