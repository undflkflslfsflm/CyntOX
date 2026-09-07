from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from oslab.database import LabDatabase
from oslab.schemas import RunManifest, utc_now
from oslab.state_machine import SupervisorState, validate_transition


class PersistentSupervisor:
    def __init__(self, database: LabDatabase, runtime_root: Path) -> None:
        self.database = database
        self.runtime_root = runtime_root.resolve()
        self.database.migrate()

    def create_run(self, manifest: RunManifest) -> str:
        now = utc_now().isoformat()
        experiment = manifest.experiment_id
        encoded = manifest.model_dump_json()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO experiments(id, created_at, config_json) VALUES (?, ?, ?)",
                (experiment, now, "{}"),
            )
            connection.execute(
                "INSERT INTO runs(id, experiment_id, state, manifest_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (manifest.run_id, experiment, SupervisorState.SELECT_TASK, encoded, now, now),
            )
            connection.execute(
                "INSERT INTO state_transitions(run_id, from_state, to_state, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (manifest.run_id, None, SupervisorState.SELECT_TASK, "{}", now),
            )
        return manifest.run_id

    def transition(
        self, run_id: str, destination: SupervisorState, payload: dict[str, Any] | None = None
    ) -> None:
        encoded = json.dumps(payload or {}, sort_keys=True)
        now = utc_now().isoformat()
        with self.database.transaction() as connection:
            row = connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            source = SupervisorState(row[0])
            validate_transition(source, destination)
            connection.execute(
                "UPDATE runs SET state=?, updated_at=? WHERE id=? AND state=?",
                (destination, now, run_id, source),
            )
            if connection.total_changes != 1:
                raise RuntimeError("concurrent state transition detected")
            connection.execute(
                "INSERT INTO state_transitions(run_id, from_state, to_state, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, source, destination, encoded, now),
            )

    def inspect_resume(self, run_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state, manifest_json, updated_at FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            transitions = connection.execute(
                "SELECT from_state, to_state, payload_json, created_at FROM state_transitions WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        resources = self._inspect_resources(run_id)
        return {
            "run_id": run_id,
            "state": row["state"],
            "updated_at": row["updated_at"],
            "manifest_hash": hashlib.sha256(row["manifest_json"].encode()).hexdigest(),
            "resources": resources,
            "transitions": [dict(item) for item in transitions],
            "resume_token": str(uuid4()),
        }

    def _inspect_resources(self, run_id: str) -> dict[str, Any]:
        run_root = self.runtime_root / "runs" / run_id
        pid_file = run_root / "qemu.pid"
        worktree_file = run_root / "worktree.txt"
        return {
            "run_root_exists": run_root.is_dir(),
            "qemu_pid_recorded": pid_file.read_text().strip() if pid_file.exists() else None,
            "worktree": worktree_file.read_text().strip() if worktree_file.exists() else None,
        }
