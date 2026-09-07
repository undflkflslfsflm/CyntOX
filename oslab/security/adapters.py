from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Adapter:
    name: str
    executable: str
    capabilities: tuple[str, ...]
    network_required: bool
    command_template: tuple[str, ...]

    def availability(self) -> dict[str, Any]:
        executable = shutil.which(self.executable)
        return {
            "name": self.name,
            "available": executable is not None,
            "executable": executable,
            "capabilities": list(self.capabilities),
            "network_required": self.network_required,
        }

    def command(self, repository: Path, output: Path) -> list[str]:
        executable = shutil.which(self.executable)
        if executable is None:
            raise FileNotFoundError(self.executable)
        values = {"repo": str(repository.resolve()), "output": str(output.resolve())}
        return [executable, *(part.format_map(values) for part in self.command_template)]


ADAPTERS = (
    Adapter(
        "codex-security",
        "codex-security",
        ("source-audit", "validation", "patching"),
        True,
        ("scan", "{repo}", "--output", "{output}"),
    ),
    Adapter(
        "visa-vvah",
        "vvah",
        ("source-audit", "sarif", "patching"),
        True,
        ("scan", "{repo}", "--output", "{output}"),
    ),
    Adapter(
        "oss-fuzz-gen",
        "oss-fuzz-gen",
        ("fuzz-target-generation", "coverage"),
        True,
        ("run", "--target", "{repo}", "--output", "{output}"),
    ),
)


def adapter_inventory() -> list[dict[str, Any]]:
    return [adapter.availability() for adapter in ADAPTERS]


def load_sarif(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != "2.1.0":
        raise ValueError("adapter output is not SARIF 2.1.0")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise ValueError("SARIF runs must be a list")
    return payload
