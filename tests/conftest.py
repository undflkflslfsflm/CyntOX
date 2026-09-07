# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_AUTOMATIC_BASETEMP: Path | None = None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Give every pytest process its own short Windows-safe temp root."""
    global _AUTOMATIC_BASETEMP
    if getattr(config.option, "basetemp", None) is not None:
        return
    root = Path(tempfile.gettempdir()) / "cyntox-pytest"
    root.mkdir(parents=True, exist_ok=True)
    _AUTOMATIC_BASETEMP = root / str(os.getpid())
    config.option.basetemp = str(_AUTOMATIC_BASETEMP)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    global _AUTOMATIC_BASETEMP
    path = _AUTOMATIC_BASETEMP
    _AUTOMATIC_BASETEMP = None
    if path is None:
        return
    expected_parent = (Path(tempfile.gettempdir()) / "cyntox-pytest").resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(expected_parent)
    except (OSError, ValueError):
        return
    shutil.rmtree(resolved, ignore_errors=True)


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
