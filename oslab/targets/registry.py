from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from oslab.config import LabConfig
from oslab.targets.manifest import inspect_target_manifest

BUILD_MARKERS = ("Makefile", "CMakeLists.txt", "Cargo.toml", "meson.build", "build.zig")
OS_MARKERS = ("kernel", "boot", "arch")


def inspect_targets(config: LabConfig, requested: Path | None = None) -> dict[str, Any]:
    fixture_source = config.project_root / "fixtures" / "boot" / "boot.asm"
    fixture = {
        "name": "fixture",
        "kind": "boot-sector",
        "status": "ready" if fixture_source.is_file() else "missing",
        "source": str(fixture_source),
        "source_sha256": (
            hashlib.sha256(fixture_source.read_bytes()).hexdigest()
            if fixture_source.is_file()
            else None
        ),
        "build_profile": "pinned Docker/NASM",
        "boot_profile": "QEMU TCG, -nic none, QMP loopback",
    }
    real = (
        _inspect_real_target(requested.resolve()) if requested is not None else _from_doctor(config)
    )
    if real.get("status") == "ready":
        gate_l = "applicable"
        minimal_input = None
        resume_command = None
    elif requested is not None:
        gate_l = "blocked_invalid_or_incomplete_target"
        minimal_input = real.get("reason", "A valid oslab-target.toml target manifest")
        resume_command = f"oslab target inspect --repo {requested.resolve()} --json"
    else:
        gate_l = "blocked_missing_external_input"
        minimal_input = (
            "A local path to the authorized OS source plus its existing build entry point"
        )
        resume_command = "oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json"
    return {
        "fixture": fixture,
        "real_os": real,
        "selected_real_os": real.get("path") if real.get("status") == "ready" else None,
        "gate_l": gate_l,
        "minimal_input": minimal_input,
        "resume_command": resume_command,
    }


def _from_doctor(config: LabConfig) -> dict[str, Any]:
    report_path = config.artifacts_root / "discovery" / "hardware-report.json"
    if report_path.is_file():
        try:
            target = json.loads(report_path.read_text(encoding="utf-8")).get("target", {})
        except json.JSONDecodeError:
            target = {}
        selected = target.get("selected")
        if isinstance(selected, str):
            return _inspect_real_target(Path(selected).resolve())
        return {
            "status": "absent",
            "path": None,
            "bounded_candidates": target.get("bounded_candidates", []),
            "reason": "doctor found no buildable OS source in the bounded discovery scope",
        }
    return {
        "status": "absent",
        "path": None,
        "bounded_candidates": [],
        "reason": "run oslab doctor; no saved bounded target discovery exists",
    }


def _inspect_real_target(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        return {"status": "invalid", "path": str(root), "reason": "path is not a directory"}
    build = [name for name in BUILD_MARKERS if (root / name).exists()]
    os_markers = [name for name in OS_MARKERS if (root / name).exists()]
    manifest = root / "oslab-target.toml"
    if not build or len(os_markers) < 2:
        return {
            "status": "insufficient",
            "path": str(root),
            "build_markers": build,
            "os_markers": os_markers,
            "manifest": str(manifest) if manifest.is_file() else None,
            "reason": "target needs an existing build marker and at least two OS-source markers",
        }
    manifest_result = inspect_target_manifest(root)
    if manifest_result["status"] == "missing":
        return {
            "status": "candidate_manifest_required",
            "path": str(root),
            "build_markers": build,
            "os_markers": os_markers,
            "manifest": None,
            "reason": "target has OS/build markers but needs a valid oslab-target.toml manifest",
        }
    if manifest_result["status"] != "ready":
        return {
            "status": "invalid_manifest",
            "path": str(root),
            "build_markers": build,
            "os_markers": os_markers,
            "manifest": str(manifest),
            "manifest_result": manifest_result,
            "reason": "target manifest failed validation",
        }
    return {
        "status": "ready",
        "path": str(root),
        "build_markers": build,
        "os_markers": os_markers,
        "manifest": str(manifest),
        "manifest_result": manifest_result,
        "integration_status": "declarative-manifest-valid",
    }
