from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from oslab.schemas import Outcome

MANIFEST_NAME = "oslab-target.toml"
PROFILE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
COMMIT_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
SHELL_EVAL_FLAGS = {
    "bash": {"-c"},
    "cmd.exe": {"/c", "/k"},
    "cmd": {"/c", "/k"},
    "node": {"-e", "--eval"},
    "powershell.exe": {"-command", "-encodedcommand", "-ec"},
    "powershell": {"-command", "-encodedcommand", "-ec"},
    "pwsh.exe": {"-command", "-encodedcommand", "-ec"},
    "pwsh": {"-command", "-encodedcommand", "-ec"},
    "python.exe": {"-c"},
    "python": {"-c"},
    "sh": {"-c"},
}

TARGET_MANIFEST_TEMPLATE = """schema_version = 1
name = "authorized-os"

[source]
root = "."
base_commit = "<immutable git commit sha>"
authorization = "owned_or_authorized"

[build.profiles.debug]
description = "Existing debug build profile; replace argv with the target's normal build entry point."
env_allowlist = ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"]

[[build.profiles.debug.commands]]
argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/build.ps1", "-Configuration", "Debug"]
cwd = "."

[[build.profiles.debug.artifacts]]
path = "build/kernel.bin"
kind = "kernel"

[boot]
method = "qemu"
readiness_patterns = ["OSLAB_READY"]
serial_log = true

[boot.qemu]
machine = "q35"
cpu = "max"
memory_mb = 1024
accelerator = "auto"
network = "none"
devices = []

[[boot.artifacts]]
path = "build/kernel.bin"
kind = "kernel"

[[tests]]
id = "smoke"
kind = "smoke"
transport = "serial"
input = "smoke"
expected_outcome = "PASS"

[debug]
symbols = "build/kernel.elf"
symbolizer = ["llvm-symbolizer"]

[instrumentation]
sanitizers = []
coverage = []

[cleanup]
paths = ["build/tmp", ".oslab-target"]
"""


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    base_commit: str = Field(min_length=7)
    authorization: Literal["owned_or_authorized"]

    @field_validator("root")
    @classmethod
    def root_is_safe_path(cls, value: str) -> str:
        return _safe_path(value, allow_absolute=True)

    @field_validator("base_commit")
    @classmethod
    def base_commit_is_immutable_sha(cls, value: str) -> str:
        if not COMMIT_SHA.fullmatch(value):
            raise ValueError("source.base_commit must be a full 40- or 64-character commit SHA")
        return value.lower()


class CommandSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    cwd: str = "."

    @field_validator("argv")
    @classmethod
    def argv_is_vector_not_shell_blob(cls, value: list[str]) -> list[str]:
        for token in value:
            if not token or "\x00" in token or "\n" in token or "\r" in token:
                raise ValueError("command argv entries must be non-empty single-line strings")
        executable = Path(value[0]).name.lower()
        blocked_flags = SHELL_EVAL_FLAGS.get(executable, set())
        used_flags = {
            token.lower() for token in value[1:] if token.startswith("-") or token.startswith("/")
        }
        if blocked_flags & used_flags:
            raise ValueError("command argv must name scripts/tools directly, not shell eval flags")
        return value

    @field_validator("cwd")
    @classmethod
    def cwd_is_relative_safe_path(cls, value: str) -> str:
        return _safe_path(value)


class BuildArtifactSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: Literal["kernel", "disk", "iso", "initrd", "firmware", "symbols", "log", "other"]

    @field_validator("path")
    @classmethod
    def path_is_relative_safe_path(cls, value: str) -> str:
        return _safe_path(value)


class BuildProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    commands: list[CommandSpec] = Field(min_length=1)
    env_allowlist: list[str] = Field(default_factory=list)
    artifacts: list[BuildArtifactSpec] = Field(default_factory=list)

    @field_validator("env_allowlist")
    @classmethod
    def env_names_are_explicit(cls, value: list[str]) -> list[str]:
        for name in value:
            if not ENV_NAME.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name}")
        return value


class BuildSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: dict[str, BuildProfileSpec] = Field(min_length=1)

    @field_validator("profiles")
    @classmethod
    def profile_names_are_stable(
        cls, value: dict[str, BuildProfileSpec]
    ) -> dict[str, BuildProfileSpec]:
        for name in value:
            if not PROFILE_NAME.fullmatch(name):
                raise ValueError(f"invalid build profile name: {name}")
        return value


class QemuSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str = "q35"
    cpu: str = "max"
    memory_mb: int = Field(ge=64, le=262_144)
    accelerator: Literal["auto", "kvm", "whpx", "tcg"] = "auto"
    network: Literal["none"]
    devices: list[str] = Field(default_factory=list)

    @field_validator("machine", "cpu")
    @classmethod
    def single_line_value(cls, value: str) -> str:
        if not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("QEMU values must be non-empty single-line strings")
        return value

    @field_validator("devices")
    @classmethod
    def devices_are_data_not_options(cls, value: list[str]) -> list[str]:
        for device in value:
            if not device.strip() or "\x00" in device or "\n" in device or "\r" in device:
                raise ValueError("QEMU devices must be non-empty single-line strings")
        return value


class BootSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["qemu"]
    qemu: QemuSpec
    artifacts: list[BuildArtifactSpec] = Field(min_length=1)
    readiness_patterns: list[str] = Field(min_length=1)
    serial_log: bool = True

    @field_validator("readiness_patterns")
    @classmethod
    def readiness_patterns_are_single_line(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not pattern or "\x00" in pattern or "\n" in pattern or "\r" in pattern:
                raise ValueError("readiness patterns must be non-empty single-line strings")
        return value


class TestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["smoke", "regression", "replay", "fuzz"]
    transport: Literal["serial", "qmp", "script"]
    input: str = ""
    expected_outcome: Outcome = Outcome.PASS

    @field_validator("id")
    @classmethod
    def id_is_stable(cls, value: str) -> str:
        if not PROFILE_NAME.fullmatch(value):
            raise ValueError("test id must contain only stable identifier characters")
        return value


class DebugSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: str | None = None
    symbolizer: list[str] = Field(default_factory=list)

    @field_validator("symbols")
    @classmethod
    def symbols_is_safe_path(cls, value: str | None) -> str | None:
        return None if value is None else _safe_path(value)


class InstrumentationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sanitizers: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class CleanupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def cleanup_paths_are_relative(cls, value: list[str]) -> list[str]:
        return [_safe_path(path) for path in value]


class TargetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    name: str
    source: SourceSpec
    build: BuildSpec
    boot: BootSpec
    tests: list[TestSpec] = Field(min_length=1)
    debug: DebugSpec = Field(default_factory=DebugSpec)
    instrumentation: InstrumentationSpec = Field(default_factory=InstrumentationSpec)
    cleanup: CleanupSpec = Field(default_factory=CleanupSpec)

    @field_validator("name")
    @classmethod
    def name_is_stable(cls, value: str) -> str:
        if not PROFILE_NAME.fullmatch(value):
            raise ValueError("target name must contain only stable identifier characters")
        return value

    @model_validator(mode="after")
    def has_smoke_test(self) -> TargetManifest:
        if not any(test.kind == "smoke" for test in self.tests):
            raise ValueError("manifest must declare at least one smoke test")
        return self


def inspect_target_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return {"status": "missing", "path": str(manifest_path)}
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return {"status": "invalid", "path": str(manifest_path), "errors": [str(exc)]}
    try:
        manifest = TargetManifest.model_validate(raw)
    except ValidationError as exc:
        return {
            "status": "invalid",
            "path": str(manifest_path),
            "errors": exc.errors(include_url=False),
        }

    path_errors = _validate_manifest_paths(root.resolve(), manifest)
    if path_errors:
        return {"status": "invalid", "path": str(manifest_path), "errors": path_errors}

    return {
        "status": "ready",
        "path": str(manifest_path),
        "name": manifest.name,
        "source_root": str(_resolve_within(root.resolve(), manifest.source.root)),
        "base_commit": manifest.source.base_commit,
        "build_profiles": sorted(manifest.build.profiles),
        "boot_method": manifest.boot.method,
        "qemu": manifest.boot.qemu.model_dump(),
        "readiness_patterns": manifest.boot.readiness_patterns,
        "smoke_tests": [test.id for test in manifest.tests if test.kind == "smoke"],
        "test_count": len(manifest.tests),
        "sha256": hashlib_file(manifest_path),
    }


def manifest_template_json() -> dict[str, Any]:
    return {
        "filename": MANIFEST_NAME,
        "content_sha256": hashlib_text(TARGET_MANIFEST_TEMPLATE),
        "content": TARGET_MANIFEST_TEMPLATE,
    }


def _validate_manifest_paths(root: Path, manifest: TargetManifest) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    source_root = _resolve_or_error(root, manifest.source.root, errors, "source.root")
    if source_root is None:
        return errors

    if not source_root.is_dir():
        errors.append({"field": "source.root", "reason": "source root is not a directory"})

    for profile_name, profile in manifest.build.profiles.items():
        for index, command in enumerate(profile.commands):
            _resolve_or_error(
                source_root,
                command.cwd,
                errors,
                f"build.profiles.{profile_name}.commands.{index}.cwd",
            )
        for index, artifact in enumerate(profile.artifacts):
            _resolve_or_error(
                source_root,
                artifact.path,
                errors,
                f"build.profiles.{profile_name}.artifacts.{index}.path",
            )

    for index, artifact in enumerate(manifest.boot.artifacts):
        _resolve_or_error(source_root, artifact.path, errors, f"boot.artifacts.{index}.path")
    if manifest.debug.symbols is not None:
        _resolve_or_error(source_root, manifest.debug.symbols, errors, "debug.symbols")
    for index, path in enumerate(manifest.cleanup.paths):
        _resolve_or_error(source_root, path, errors, f"cleanup.paths.{index}")
    return errors


def _resolve_or_error(
    root: Path, value: str, errors: list[dict[str, Any]], field: str
) -> Path | None:
    try:
        return _resolve_within(root, value)
    except ValueError as exc:
        errors.append({"field": field, "reason": str(exc)})
        return None


def _resolve_within(root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escapes target source root")
    return resolved


def _safe_path(value: str, allow_absolute: bool = False) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or "\x00" in normalized:
        raise ValueError("path must be non-empty")
    if path.is_absolute() and not allow_absolute:
        raise ValueError("path must be relative")
    if ".." in path.parts:
        raise ValueError("path traversal is not allowed")
    return value


def hashlib_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashlib_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def manifest_to_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
