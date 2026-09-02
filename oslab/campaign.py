from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from oslab.artifacts import ArtifactStore
from oslab.config import LabConfig
from oslab.database import LabDatabase
from oslab.process_runner import SafeProcessRunner
from oslab.schemas import ModelIdentity, ResourceBudget, RunManifest, utc_now
from oslab.state_machine import SupervisorState
from oslab.supervisor import PersistentSupervisor

HAPPY_PATH = (
    SupervisorState.SELECT_TASK,
    SupervisorState.PREPARE_WORKTREE,
    SupervisorState.BUILD_BASELINE,
    SupervisorState.BOOT_BASELINE,
    SupervisorState.GENERATE_HYPOTHESES,
    SupervisorState.RANK_HYPOTHESES,
    SupervisorState.GENERATE_TEST,
    SupervisorState.EXECUTE_TEST,
    SupervisorState.TRIAGE_RESULT,
    SupervisorState.REPRODUCE,
    SupervisorState.MINIMIZE,
    SupervisorState.ROOT_CAUSE_ANALYSIS,
    SupervisorState.PROPOSE_PATCHES,
    SupervisorState.VERIFY_TARGETED_TEST,
    SupervisorState.RUN_REGRESSION,
    SupervisorState.ADVERSARIAL_VERIFY,
    SupervisorState.RECORD_FINDING,
    SupervisorState.CLEANUP,
    SupervisorState.COMPLETE,
)


def fixture_manifest(config: LabConfig, run_id: str, seed: int) -> RunManifest:
    hardware = config.artifacts_root / "discovery" / "hardware-report.json"
    hardware_hash = (
        hashlib.sha256(hardware.read_bytes()).hexdigest() if hardware.is_file() else "unavailable"
    )
    return RunManifest(
        run_id=run_id,
        experiment_id="supervisor-recovery-proof",
        repository_commit=_git_commit(config.project_root),
        dirty_state_hash=_dirty_hash(config.project_root),
        target="fixture",
        profile="recovery-proof",
        model=ModelIdentity(
            provider=config.model.provider,
            runtime="Ollama",
            model_id=config.model.model_id,
            endpoint=config.model.endpoint,
        ),
        model_parameters={"seed": seed, "live_call": False},
        component_versions={"supervisor_schema": "1"},
        prompt_hashes={},
        artifact_hashes={},
        seed=seed,
        budget=ResourceBudget(wall_seconds=60, tool_calls=32),
        hardware_snapshot_hash=hardware_hash,
    )


def advance_run(database: LabDatabase, runtime_root: Path, run_id: str, max_steps: int) -> int:
    supervisor = PersistentSupervisor(database, runtime_root)
    advanced = 0
    while advanced < max_steps:
        snapshot = supervisor.inspect_resume(run_id)
        current = SupervisorState(snapshot["state"])
        if current in {
            SupervisorState.COMPLETE,
            SupervisorState.BLOCKED,
            SupervisorState.CANCELLED,
        }:
            break
        if current == SupervisorState.RECORD_FINDING:
            _record_finding_once(database, run_id)
        index = HAPPY_PATH.index(current)
        destination = HAPPY_PATH[index + 1]
        supervisor.transition(
            run_id,
            destination,
            {
                "checkpoint": index + 1,
                "resumed": advanced == 0 and current != SupervisorState.SELECT_TASK,
            },
        )
        advanced += 1
    return advanced


async def prove_recovery(config: LabConfig, seed: int = 17) -> dict[str, Any]:
    database = LabDatabase(config.runtime_root / "oslab.sqlite3")
    supervisor = PersistentSupervisor(database, config.runtime_root)
    run_id = str(uuid4())
    supervisor.create_run(fixture_manifest(config, run_id, seed))
    command = [
        sys.executable,
        "-m",
        "oslab.campaign_worker",
        "--database",
        str(database.path),
        "--runtime-root",
        str(config.runtime_root),
        "--run-id",
        run_id,
    ]
    runner = SafeProcessRunner()
    crashed = await runner.run(
        [*command, "--max-steps", "6", "--crash-after", "6"],
        cwd=config.project_root,
        timeout=30,
    )
    if crashed.returncode != 97 or crashed.timed_out:
        raise RuntimeError(f"controlled supervisor crash failed: {crashed}")
    interrupted = supervisor.inspect_resume(run_id)
    if interrupted["state"] != SupervisorState.GENERATE_TEST:
        raise RuntimeError(f"unexpected interrupted state: {interrupted['state']}")
    resumed = await runner.run(
        [*command, "--max-steps", "64"],
        cwd=config.project_root,
        timeout=30,
    )
    if resumed.returncode != 0 or resumed.timed_out:
        raise RuntimeError(f"supervisor resume failed: {resumed}")
    final = supervisor.inspect_resume(run_id)
    if final["state"] != SupervisorState.COMPLETE:
        raise RuntimeError(f"supervisor did not complete: {final['state']}")
    with database.connect() as connection:
        finding_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM findings WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        transition_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM state_transitions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
    if finding_count != 1 or transition_count != len(HAPPY_PATH):
        raise RuntimeError(
            f"non-idempotent recovery: findings={finding_count}, transitions={transition_count}"
        )
    proof = {
        "run_id": run_id,
        "controlled_exit_code": crashed.returncode,
        "interrupted_state": interrupted["state"],
        "final_state": final["state"],
        "finding_count": finding_count,
        "transition_count": transition_count,
        "integrity": database.integrity_check(),
        "crash_command": list(crashed.argv),
        "resume_command": list(resumed.argv),
        "completed_at": utc_now().isoformat(),
    }
    record = ArtifactStore(config.artifacts_root).put_json(
        proof, f"supervisor-recovery-{run_id}.json"
    )
    return {**proof, "artifact_sha256": record.sha256}


def _record_finding_once(database: LabDatabase, run_id: str) -> None:
    finding_id = f"fixture-seeded-{run_id}"
    with database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO findings(id, run_id, fingerprint, outcome, reproducer_hash, "
            "patch_hash, verification_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding_id,
                run_id,
                "fixture-seeded-calculation-v1",
                "PASS",
                "seeded-command-B",
                f"mov-al-4-{run_id}",
                json.dumps({"targeted": "PASS", "regression": "PASS"}, sort_keys=True),
                utc_now().isoformat(),
            ),
        )


def _git_commit(root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return "unavailable"
    result = subprocess.run(  # noqa: S603 - resolved git binary and constant arguments
        [git, "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unborn"


def _dirty_hash(root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return "unavailable"
    result = subprocess.run(  # noqa: S603 - resolved git binary and constant arguments
        [git, "status", "--porcelain=v1"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return hashlib.sha256(result.stdout.encode()).hexdigest()
