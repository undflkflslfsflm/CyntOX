from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    CRASH = "CRASH"
    HANG = "HANG"
    BUILD_ERROR = "BUILD_ERROR"
    BOOT_ERROR = "BOOT_ERROR"
    TEST_ERROR = "TEST_ERROR"
    INFRA_ERROR = "INFRA_ERROR"
    POLICY_DENIED = "POLICY_DENIED"
    CANCELLED = "CANCELLED"
    EVALUATOR_EXPLOIT = "EVALUATOR_EXPLOIT"
    INVALID_SOLUTION = "INVALID_SOLUTION"


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    PROCESS = "process"
    VALIDATION = "validation"
    POLICY = "policy"
    MODEL = "model"
    BUILD = "build"
    VM = "vm"
    UNSUPPORTED = "unsupported"
    INTERNAL = "internal"


class ClassifiedError(BaseModel):
    kind: ErrorKind
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Outcome
    run_id: str
    tool: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int = Field(ge=0)
    hashes: dict[str, str] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    error: ClassifiedError | None = None

    @field_validator("ended_at")
    @classmethod
    def end_not_before_start(cls, value: datetime, info: Any) -> datetime:
        start = info.data.get("started_at")
        if start is not None and value < start:
            raise ValueError("ended_at precedes started_at")
        return value


class ResourceBudget(BaseModel):
    wall_seconds: float = Field(default=600.0, gt=0)
    cpu_seconds: float = Field(default=600.0, gt=0)
    memory_mb: int = Field(default=4096, ge=64)
    disk_mb: int = Field(default=4096, ge=16)
    processes: int = Field(default=32, ge=1)
    output_bytes: int = Field(default=2_000_000, ge=1024)
    tool_calls: int = Field(default=100, ge=1)
    input_tokens: int = Field(default=32_768, ge=1)
    output_tokens: int = Field(default=8_192, ge=1)


class ModelIdentity(BaseModel):
    provider: str
    runtime: str
    runtime_version: str | None = None
    model_id: str
    architecture: str | None = None
    parameters: int | None = None
    quantization: str | None = None
    format: str | None = None
    context_limit: int | None = None
    endpoint: str
    capabilities: list[str] = Field(default_factory=list)


class ModelUsage(BaseModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    prompt_eval_seconds: float = Field(default=0.0, ge=0)
    eval_seconds: float = Field(default=0.0, ge=0)


class ModelResponse(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    content: str
    structured: dict[str, Any] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model: ModelIdentity
    prompt_hash: str
    response_hash: str
    started_at: datetime
    ended_at: datetime
    seed: int | None = None


class RunManifest(BaseModel):
    schema_version: int = 1
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    experiment_id: str
    repository_commit: str
    dirty_state_hash: str
    target: str
    profile: str
    model: ModelIdentity
    model_parameters: dict[str, Any]
    component_versions: dict[str, str]
    prompt_hashes: dict[str, str]
    artifact_hashes: dict[str, str]
    seed: int
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    budget: ResourceBudget
    hardware_snapshot_hash: str
    outcome: Outcome | None = None


class TrajectoryEvent(BaseModel):
    schema_version: int = 1
    trajectory_id: str
    sequence: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=utc_now)
    kind: str
    role: str
    content: dict[str, Any]
    evidence_hashes: list[str] = Field(default_factory=list)
    verified: bool = False
    label: Outcome | None = None
