from __future__ import annotations

import json
from typing import Any

from oslab.database import LabDatabase
from oslab.schemas import utc_now
from oslab.security.models import AuditRun, AuditState, SecurityFinding


class SecurityAuditStore:
    def __init__(self, database: LabDatabase) -> None:
        self.database = database
        self.database.migrate()

    def create(self, audit: AuditRun) -> None:
        now = audit.created_at.isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO security_audits(id, repository, repository_hash, base_commit, "
                "dirty_state_hash, profile, state, config_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit.id,
                    audit.repository,
                    audit.repository_hash,
                    audit.base_commit,
                    audit.dirty_state_hash,
                    audit.profile,
                    audit.state,
                    audit.model_dump_json(),
                    now,
                    now,
                ),
            )

    def set_state(self, audit_id: str, state: AuditState, *, error: str | None = None) -> None:
        audit = self.get(audit_id)
        audit.state = state
        audit.error = error
        audit.updated_at = utc_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE security_audits SET state=?, config_json=?, updated_at=? WHERE id=?",
                (state, audit.model_dump_json(), audit.updated_at.isoformat(), audit_id),
            ).rowcount
        if changed != 1:
            raise KeyError(audit_id)

    def checkpoint(self, audit_id: str, stage: str, state: str, payload: dict[str, Any]) -> None:
        now = utc_now().isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO security_audit_stages(audit_id, stage, state, payload_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(audit_id, stage) DO UPDATE SET "
                "state=excluded.state, payload_json=excluded.payload_json, "
                "updated_at=excluded.updated_at",
                (audit_id, stage, state, json.dumps(payload, sort_keys=True), now),
            )

    def stage(self, audit_id: str, stage: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state, payload_json, updated_at FROM security_audit_stages "
                "WHERE audit_id=? AND stage=?",
                (audit_id, stage),
            ).fetchone()
        if row is None:
            return None
        return {
            "state": row["state"],
            "payload": json.loads(row["payload_json"]),
            "updated_at": row["updated_at"],
        }

    def record_finding(self, finding: SecurityFinding) -> None:
        now = utc_now().isoformat()
        finding.updated_at = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO security_findings(id, audit_id, fingerprint, status, severity, "
                "payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(audit_id, fingerprint) DO UPDATE SET status=excluded.status, "
                "severity=excluded.severity, payload_json=excluded.payload_json, "
                "updated_at=excluded.updated_at",
                (
                    finding.id,
                    finding.audit_id,
                    finding.fingerprint,
                    finding.status,
                    finding.severity,
                    finding.model_dump_json(),
                    finding.created_at.isoformat(),
                    now,
                ),
            )

    def findings(self, audit_id: str, status: str | None = None) -> list[SecurityFinding]:
        sql = "SELECT payload_json FROM security_findings WHERE audit_id=?"
        parameters: list[str] = [audit_id]
        if status is not None:
            sql += " AND status=?"
            parameters.append(status)
        sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        sql += "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, created_at"
        with self.database.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [SecurityFinding.model_validate_json(row["payload_json"]) for row in rows]

    def get(self, audit_id: str) -> AuditRun:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT config_json FROM security_audits WHERE id=?", (audit_id,)
            ).fetchone()
        if row is None:
            raise KeyError(audit_id)
        return AuditRun.model_validate_json(row["config_json"])

    def list(self, limit: int = 20) -> list[AuditRun]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT config_json FROM security_audits ORDER BY updated_at DESC LIMIT ?",
                (min(max(limit, 1), 100),),
            ).fetchall()
        return [AuditRun.model_validate_json(row["config_json"]) for row in rows]

    def status(self, audit_id: str) -> dict[str, Any]:
        audit = self.get(audit_id)
        with self.database.connect() as connection:
            stages = [
                dict(row)
                for row in connection.execute(
                    "SELECT stage, state, updated_at FROM security_audit_stages "
                    "WHERE audit_id=? ORDER BY rowid",
                    (audit_id,),
                ).fetchall()
            ]
        return {
            "audit": audit.model_dump(mode="json"),
            "stages": stages,
            "finding_count": len(self.findings(audit_id)),
        }
