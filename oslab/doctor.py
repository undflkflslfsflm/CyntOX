from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from oslab.config import LabConfig

TOOLS = (
    "python",
    "uv",
    "git",
    "qemu-system-x86_64",
    "qemu-img",
    "gdb",
    "gcc",
    "clang",
    "cmake",
    "ninja",
    "docker",
    "podman",
    "wsl",
    "ollama",
    "qwen",
    "qwen-code",
    "node",
)


def _run(argv: list[str], timeout: float = 10.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(  # noqa: S603 - argv comes from the fixed doctor registry
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env={
                key: value
                for key, value in os.environ.items()
                if key.upper() not in {"OPENAI_API_KEY", "API_KEY", "TOKEN"}
            },
        )
        return {
            "argv": argv,
            "exit_code": completed.returncode,
            "stdout": completed.stdout.strip()[:8192],
            "stderr": completed.stderr.strip()[:8192],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "error": type(exc).__name__, "message": str(exc)}


def _version(path: str | None) -> dict[str, Any]:
    if path is None:
        return {"present": False}
    result = _run([path, "--version"], 5)
    first = (result.get("stdout") or result.get("stderr") or "").splitlines()
    return {"present": True, "path": path, "version": first[0] if first else None}


def _disk_benchmark(root: Path, size_mb: int = 16) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    data = bytes(1024 * 1024)
    handle, name = tempfile.mkstemp(prefix="doctor-disk-", suffix=".bin", dir=root)
    os.close(handle)
    path = Path(name)
    try:
        start = time.perf_counter()
        with path.open("wb", buffering=0) as stream:
            for _ in range(size_mb):
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        write_seconds = time.perf_counter() - start
        start = time.perf_counter()
        with path.open("rb", buffering=0) as stream:
            while stream.read(1024 * 1024):
                pass
        read_seconds = time.perf_counter() - start
        return {
            "bytes": size_mb * 1024 * 1024,
            "write_mib_s": round(size_mb / max(write_seconds, 0.000001), 2),
            "read_mib_s": round(size_mb / max(read_seconds, 0.000001), 2),
            "scope": "small temporary sequential safety probe",
        }
    finally:
        path.unlink(missing_ok=True)


def _gpu() -> dict[str, Any]:
    nvidia = shutil.which("nvidia-smi")
    if not nvidia:
        return {"present": False}
    query = _run(
        [
            nvidia,
            "--query-gpu=name,memory.total,memory.used,driver_version,power.limit,utilization.gpu,compute_cap",
            "--format=csv,noheader,nounits",
        ]
    )
    fields = [part.strip() for part in str(query.get("stdout", "")).split(",")]
    names = [
        "name",
        "memory_total_mib",
        "memory_used_mib",
        "driver",
        "power_limit_w",
        "utilization_percent",
        "compute_capability",
    ]
    return {
        "present": query.get("exit_code") == 0,
        "nvidia_smi": nvidia,
        **dict(zip(names, fields, strict=False)),
    }


def _disks() -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        values.append(
            {
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "filesystem": partition.fstype,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
            }
        )
    return values


def _virtualization(tooling: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"accelerators": [], "hypervisor_present": None}
    if platform.system() == "Windows":
        cim = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent",
            ]
        )
        result["hypervisor_present"] = str(cim.get("stdout", "")).strip().lower() == "true"
        result["whpx"] = bool(tooling.get("qemu-system-x86_64", {}).get("present"))
    qemu = tooling.get("qemu-system-x86_64", {}).get("path")
    if qemu:
        accel = _run([qemu, "-accel", "help"])
        result["accelerators"] = str(accel.get("stdout", "")).splitlines()
    docker = tooling.get("docker", {})
    if docker.get("present"):
        info = _run([docker["path"], "info", "--format", "{{.OSType}}/{{.Architecture}}"], 20)
        result["container_runtime"] = info
        if info.get("exit_code") == 0:
            result["accelerators"].append("tcg-via-pinned-linux-container")
    return result


def _model_files() -> list[dict[str, Any]]:
    home = Path.home()
    manifest_root = home / ".ollama" / "models" / "manifests"
    models: list[dict[str, Any]] = []
    if manifest_root.exists():
        for path in manifest_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            models.append(
                {
                    "runtime": "ollama",
                    "manifest": str(path),
                    "model_id": ":".join(path.relative_to(manifest_root).parts[-2:]),
                    "layers": value.get("layers", []),
                }
            )
    return models


def _target_scan(root: Path) -> dict[str, Any]:
    markers = {
        "Cargo.toml",
        "Makefile",
        "CMakeLists.txt",
        "meson.build",
        "build.zig",
        "kernel",
        "boot",
        "arch",
    }
    candidates = [root, root.parent]
    with suppress(OSError):
        candidates.extend(path for path in root.parent.iterdir() if path.is_dir())
    seen: set[Path] = set()
    findings: list[dict[str, Any]] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        present = sorted(name for name in markers if (resolved / name).exists())
        if present:
            findings.append({"path": str(resolved), "markers": present})
    real = [item for item in findings if Path(item["path"]) != root and len(item["markers"]) >= 2]
    return {
        "project_role": "dedicated-orchestrator",
        "bounded_candidates": findings,
        "real_os_present": bool(real),
        "selected": real[0]["path"] if len(real) == 1 else None,
    }


def collect_report(config: LabConfig) -> dict[str, Any]:
    tooling = {name: _version(shutil.which(name)) for name in TOOLS}
    memory = psutil.virtual_memory()
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "host": {
            "os": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
            "shell": os.environ.get("COMSPEC") or os.environ.get("SHELL"),
            "python": platform.python_version(),
        },
        "cpu": {
            "model": platform.processor(),
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_threads": psutil.cpu_count(logical=True),
        },
        "memory": {"total_bytes": memory.total, "available_bytes": memory.available},
        "gpu": _gpu(),
        "disks": _disks(),
        "safe_disk_benchmark": _disk_benchmark(config.runtime_root / "doctor"),
        "tooling": tooling,
        "virtualization": _virtualization(tooling),
        "model_files": _model_files(),
        "model_endpoint": {
            "configured": config.model.endpoint,
            "model_id": config.model.model_id,
            "loopback_only": True,
        },
        "qwen_code": {
            "present": tooling["qwen"].get("present") or tooling["qwen-code"].get("present"),
            "project_settings": str(config.project_root / ".qwen" / "settings.json"),
            "global_settings_inspected": False,
            "reason": "global configuration is not read to avoid exposing credentials",
        },
        "target": _target_scan(config.project_root),
    }
    return report


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def write_report(config: LabConfig, report: dict[str, Any]) -> dict[str, Path]:
    json_path = config.project_root / "artifacts" / "discovery" / "hardware-report.json"
    markdown_path = config.project_root / "docs" / "HARDWARE_REPORT.md"
    toml_path = config.project_root / "config" / "local.auto.toml"
    _atomic_text(json_path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    gpu = report["gpu"]
    markdown = f"""# Hardware Report

Generated: `{report["generated_at"]}`

## Host

- OS: {report["host"]["os"]} {report["host"]["release"]} ({report["host"]["architecture"]})
- CPU: {report["cpu"]["model"]} — {report["cpu"]["physical_cores"]} cores / {report["cpu"]["logical_threads"]} threads
- RAM: {report["memory"]["total_bytes"] / 2**30:.2f} GiB total; {report["memory"]["available_bytes"] / 2**30:.2f} GiB available during discovery
- GPU: {gpu.get("name", "not detected")} — {gpu.get("memory_total_mib", "unknown")} MiB VRAM, driver {gpu.get("driver", "unknown")}

## Runtime and virtualization

- Local model endpoint: `{report["model_endpoint"]["configured"]}` (loopback)
- Model ID: `{report["model_endpoint"]["model_id"]}`
- QEMU accelerators: {", ".join(report["virtualization"]["accelerators"]) or "none detected"}
- Docker: {"available" if report["tooling"]["docker"]["present"] else "not found"}
- Qwen Code: {"available" if report["qwen_code"]["present"] else "not installed"}

## Target

- Role: {report["target"]["project_role"]}
- Real OS source found: {report["target"]["real_os_present"]}
- Selected target: {report["target"]["selected"] or "none"}

The JSON report at `artifacts/discovery/hardware-report.json` is authoritative. The disk measurement is a bounded temporary sequential probe, not a storage benchmark suite.
"""
    _atomic_text(markdown_path, markdown)
    toml = f"""# Generated by `oslab doctor`; local machine facts only.
[model]
provider = "ollama"
endpoint = "{config.model.endpoint}"
model_id = "{config.model.model_id}"
timeout_seconds = {config.model.timeout_seconds}
context_tokens = {config.model.context_tokens}
output_tokens = {config.model.output_tokens}
temperature = {config.model.temperature}

[lab]
guest_network = "none"
bind_host = "127.0.0.1"
"""
    _atomic_text(toml_path, toml)
    return {"json": json_path, "markdown": markdown_path, "toml": toml_path}
