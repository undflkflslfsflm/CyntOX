from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oslab.schemas import ResourceBudget


class ModelConfig(BaseModel):
    endpoint: str = "http://127.0.0.1:11434"
    model_id: str = "huihui-qwen3.8-27b-abliterated:latest"
    provider: str = "ollama"
    timeout_seconds: float = Field(default=180.0, gt=0)
    retry_attempts: int = Field(default=3, ge=1, le=5)
    retry_backoff_seconds: float = Field(default=0.25, ge=0, le=5)
    context_tokens: int = Field(default=32768, ge=512)
    output_tokens: int = Field(default=8192, ge=32)
    temperature: float = Field(default=0.0, ge=0, le=2)

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_loopback(cls, value: str) -> str:
        allowed = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
        if not value.startswith(allowed):
            raise ValueError("model endpoint must be loopback")
        return value.rstrip("/")


class LabConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_root: Path
    runtime_root: Path
    artifacts_root: Path
    allowed_roots: list[Path]
    evaluator_roots: list[Path]
    model: ModelConfig = Field(default_factory=ModelConfig)
    budget: ResourceBudget = Field(default_factory=ResourceBudget)
    guest_network: str = "none"
    bind_host: str = "127.0.0.1"

    @field_validator("guest_network")
    @classmethod
    def network_must_be_none(cls, value: str) -> str:
        if value != "none":
            raise ValueError("v1 requires guest_network='none'")
        return value

    @field_validator("bind_host")
    @classmethod
    def bind_must_be_loopback(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("lab APIs must bind to loopback")
        return value


def default_config(project_root: Path | None = None) -> LabConfig:
    root = (project_root or Path.cwd()).resolve()
    return LabConfig(
        project_root=root,
        runtime_root=root / ".oslab",
        artifacts_root=root / "artifacts",
        allowed_roots=[root, root / ".oslab"],
        evaluator_roots=[root / ".oslab" / "evaluator", root / "tests" / "hidden"],
    )


def load_config(path: Path | None = None, project_root: Path | None = None) -> LabConfig:
    cfg = default_config(project_root)
    selected = path or cfg.project_root / "config" / "local.auto.toml"
    if not selected.exists():
        return cfg
    raw: dict[str, Any]
    with selected.open("rb") as handle:
        raw = tomllib.load(handle)
    data = cfg.model_dump()
    if "model" in raw:
        data["model"].update(raw["model"])
    if "lab" in raw:
        data.update(raw["lab"])
    return LabConfig.model_validate(data)


def safe_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "COMSPEC",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "LANG",
        "LC_ALL",
        "TERM",
        "USERPROFILE",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if extra:
        env.update(extra)
    return env
