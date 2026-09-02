from __future__ import annotations

import shutil
import subprocess

import pytest


def docker_daemon_ready() -> tuple[bool, str]:
    docker = shutil.which("docker")
    if not docker:
        return False, "docker executable was not found on PATH"
    try:
        result = subprocess.run(  # noqa: S603 - fixed diagnostic argv, resolved executable
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker daemon probe failed: {exc}"
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "docker daemon is not reachable").strip()
        return False, reason
    return True, result.stdout.strip()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-qemu",
        action="store_true",
        default=False,
        help="run Docker/QEMU e2e tests when Docker Desktop is reachable",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    qemu_items = [item for item in items if "qemu" in item.keywords]
    if not qemu_items:
        return
    if not config.getoption("--run-qemu"):
        skip = pytest.mark.skip(reason="QEMU e2e tests require --run-qemu")
        for item in qemu_items:
            item.add_marker(skip)
        return
    ready, reason = docker_daemon_ready()
    if ready:
        return
    skip = pytest.mark.skip(reason=f"QEMU e2e tests require reachable Docker daemon: {reason}")
    for item in qemu_items:
        item.add_marker(skip)
