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
    for relative in REQUIRED_ARTIFACTS:
        if relative == "artifacts/ARTIFACT_INDEX.snapshot.json":
            continue
        _write(root / relative, "{}\n" if relative.endswith(".json") else "evidence\n")
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
