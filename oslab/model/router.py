from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from oslab.config import LabConfig, ModelConfig
from oslab.model.ollama import OllamaProvider


class WorkloadKind(StrEnum):
    MODEL = "model"
    BUILD = "build"
    QEMU = "qemu"
    FUZZ = "fuzz"
    DISK_HEAVY_OFFLOAD = "disk-heavy-offload"


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    provider: str
    model_id: str
    context_tokens: int
    output_tokens: int
    enabled: bool
    reason: str


class ModelRouter:
    def __init__(self, config: LabConfig, installed_runtimes: set[str] | None = None) -> None:
        self.config = config
        self.installed_runtimes = installed_runtimes or {"ollama"}

    def profiles(self) -> dict[str, RuntimeProfile]:
        model = self.config.model
        context_limit = max(model.context_tokens, 8192)
        return {
            "fast": RuntimeProfile(
                "fast",
                model.provider,
                model.model_id,
                min(context_limit, 16_384),
                min(model.output_tokens, 2048),
                "ollama" in self.installed_runtimes,
                "resident Ollama Qwen worker; default for builds, tests, and fix loops",
            ),
            "deep": RuntimeProfile(
                "deep",
                model.provider,
                model.model_id,
                min(max(context_limit, 16_384), 32_768),
                min(max(model.output_tokens, 4096), 8192),
                "ollama" in self.installed_runtimes,
                "same resident model with larger context/output budget for hard cases",
            ),
            "long": RuntimeProfile(
                "long",
                model.provider,
                model.model_id,
                min(max(context_limit, 32_768), 65_536),
                min(max(model.output_tokens, 4096), 8192),
                "ollama" in self.installed_runtimes,
                "use sparingly; retrieval is preferred before expanding context",
            ),
            "oracle": RuntimeProfile(
                "oracle",
                "disabled",
                "",
                0,
                0,
                False,
                "no compatible larger/offloaded runtime and model files were detected locally",
            ),
        }

    def provider(self, profile: str = "fast") -> OllamaProvider:
        selected = self.profiles().get(profile)
        if selected is None:
            raise ValueError(f"unknown model profile: {profile}")
        if not selected.enabled:
            raise RuntimeError(f"model profile is disabled: {profile}: {selected.reason}")
        model = self.config.model.model_copy(
            update={
                "context_tokens": selected.context_tokens,
                "output_tokens": selected.output_tokens,
            }
        )
        return OllamaProvider(ModelConfig.model_validate(model))


class ResourceScheduler:
    _CONFLICTS = {
        frozenset({WorkloadKind.DISK_HEAVY_OFFLOAD, WorkloadKind.QEMU}),
        frozenset({WorkloadKind.DISK_HEAVY_OFFLOAD, WorkloadKind.FUZZ}),
        frozenset({WorkloadKind.DISK_HEAVY_OFFLOAD, WorkloadKind.BUILD}),
    }

    def can_run_together(self, left: WorkloadKind, right: WorkloadKind) -> bool:
        return frozenset({left, right}) not in self._CONFLICTS

    def admission_reason(self, running: list[WorkloadKind], requested: WorkloadKind) -> str:
        for workload in running:
            if not self.can_run_together(workload, requested):
                return (
                    f"defer {requested}: conflicts with active {workload}; "
                    "prevents disk-heavy offload from competing with QEMU/build/fuzz I/O"
                )
        return "admit"
