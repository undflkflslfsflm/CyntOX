from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oslab.schemas import utc_now


class AuditProfile(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class AuditState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FindingStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class SourceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    line: int = Field(ge=1)
    column: int = Field(default=1, ge=1)
    snippet_hash: str = Field(min_length=64, max_length=64)

    @field_validator("path")
    @classmethod
    def relative_safe_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not value:
            raise ValueError("finding locations must be relative paths")
        return path.as_posix()


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["static_trace", "reproducer", "test", "analyzer", "adversarial_review"]
    summary: str = Field(min_length=1, max_length=2000)
    artifact_sha256: str | None = None
    deterministic: bool = False


class ValidationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validator: str
    verdict: Literal["accept", "reject", "needs_review"]
    reason: str = Field(min_length=1, max_length=2000)
    checked_at: datetime = Field(default_factory=utc_now)


class PatchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff: str
    diff_sha256: str = Field(min_length=64, max_length=64)
    worktree: str
    targeted_check: str
    regression_check: str
    verified: bool = False


class AuditTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    audit_id: str
    stage: str
    lens: str
    state: Literal["queued", "running", "complete", "failed"] = "queued"
    attempts: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class SecurityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str
    audit_id: str
    fingerprint: str = Field(min_length=64, max_length=64)
    status: FindingStatus
    severity: Severity
    title: str
    description: str
    rule_id: str
    cwe: str | None = None
    locations: list[SourceLocation] = Field(min_length=1)
    reachability: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    validators: list[ValidationVerdict] = Field(default_factory=list)
    patch: PatchCandidate | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str
    repository: str
    repository_hash: str = Field(min_length=64, max_length=64)
    base_commit: str
    dirty_state_hash: str = Field(min_length=64, max_length=64)
    profile: AuditProfile
    state: AuditState = AuditState.QUEUED
    stages: list[str]
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: str | None = None
    report_paths: dict[str, str] = Field(default_factory=dict)
