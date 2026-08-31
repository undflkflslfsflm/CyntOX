from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from oslab.acceptance import (
    EXPECTED_GATE_SUMMARY,
    REQUIRED_ARTIFACTS,
    REQUIRED_DOCS,
    REQUIRED_SUPPORT_FILES,
    audit_acceptance,
)


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


def _create_complete_fixture_proof(root: Path) -> None:
    _git(root, "init")
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
                "proof_sha256": "a" * 64,
            },
            {
                "scope": "clean checkout",
                "command": "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\\bootstrap.ps1",
                "exit_code": 0,
                "worktree": ".oslab\\clean-checkouts\\fixture",
            },
            {
                "scope": "clean checkout",
                "command": ".venv\\Scripts\\python.exe -m oslab.cli selftest --live --json",
                "exit_code": 0,
                "proof_sha256": "b" * 64,
            },
        ],
        "gate_summary": EXPECTED_GATE_SUMMARY,
        "model": {
            "runtime": "Ollama",
            "model_id": "huihui-qwen3.8-27b-abliterated:latest",
            "parameters": 27320697856,
            "quantization": "Q4_K_M",
        },
        "qwen_code": {
            "smoke_result": "MCP_BUDGET_OK",
            "visible_tools": [
                "mcp__oslab__policy_remaining_budget",
                "mcp__oslab__fixture_explain",
            ],
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
