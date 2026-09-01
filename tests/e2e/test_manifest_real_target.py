from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from oslab.artifacts import ArtifactStore
from oslab.process_runner import SafeProcessRunner
from oslab.schemas import Outcome
from oslab.targets import manifest_template_json, run_manifest_smoke

BOOT_SECTOR_B64 = "+jHAjtiOwI7QvAB8++iOAL7wfOjCADHb6MgAPFB0GjxGdB48Q3QiPEh0KDxJdC48QnRFPFF0WOvfvhZ96JkA69e+O33okQDrz75gfeiJAA8L6/6+hn3ofwD69Ov9/sO+sX3ocwCI2AQw6F0AsA3oWACwCuhTAOugvr596FoAsDXoRgCwDehBALAK6DwA64m+z33oQwC6BAa4ACDv+vS6+QMwwO66+wOwgO66+AOwAe66+QMwwO66+wOwA+66+gOwx+66/AOwC+7DULr9A+yoIHT4WLr4A+7DrITAdAXo6f/r9sO6/QPsqAF0+7r4A+zDT1NMQUJfRVZUIHsiZXZlbnQiOiJSRUFEWSIsInNlcSI6MH0NCgBPU0xBQl9FVlQgeyJldmVudCI6IlBBU1MiLCJzZXEiOjF9DQoAT1NMQUJfRVZUIHsiZXZlbnQiOiJGQUlMIiwic2VxIjoxfQ0KAE9TTEFCX0VWVCB7ImV2ZW50IjoiQ1JBU0giLCJzZXEiOjF9DQoAT1NMQUJfRVZUIHsiZXZlbnQiOiJIQU5HX0FSTUVEIiwic2VxIjoxfQ0KAE9TTEFCX1NUQVRFIABPU0xBQl9CVUdfVkFMVUUgAE9TTEFCX0VWVCB7ImV2ZW50IjoiU0hVVERPV04iLCJzZXEiOjJ9DQoAAAAAAAAAVao="


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - resolved git binary with fixed test argv
        [git, *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _create_manifest_boot_repo(root: Path) -> Path:
    root.mkdir()
    (root / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    (root / "kernel").mkdir()
    (root / "kernel" / ".keep").write_text("", encoding="utf-8")
    (root / "boot").mkdir()
    (root / "boot" / ".keep").write_text("", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "build_disk.py").write_text(
        "\n".join(
            [
                "import base64",
                "from pathlib import Path",
                f"boot = base64.b64decode({BOOT_SECTOR_B64!r})",
                "Path('build').mkdir(exist_ok=True)",
                "Path('build/disk.raw').write_bytes(boot + (b'\\0' * (1024 * 1024 - len(boot))))",
                "print('manifest-qemu-disk-built')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(root, "init")
    _git(root, "config", "user.email", "oslab@example.invalid")
    _git(root, "config", "user.name", "OS Lab Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "seed manifest boot target")
    commit = _git(root, "rev-parse", "HEAD")
    manifest = (
        manifest_template_json()["content"]
        .replace("<immutable git commit sha>", commit)
        .replace(
            'argv = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/build.ps1", "-Configuration", "Debug"]',
            f'argv = [{json.dumps(sys.executable)}, "scripts/build_disk.py"]',
        )
        .replace('path = "build/kernel.bin"', 'path = "build/disk.raw"')
        .replace('kind = "kernel"', 'kind = "disk"')
        .replace(
            'readiness_patterns = ["OSLAB_READY"]', 'readiness_patterns = [\'"event":"READY"\']'
        )
        .replace('input = "smoke"', 'input = "P"')
        .replace(
            'success_patterns = ["OSLAB_SMOKE_PASS"]', 'success_patterns = [\'"event":"PASS"\']'
        )
    )
    (root / "oslab-target.toml").write_text(manifest, encoding="utf-8")
    return root


@pytest.mark.qemu
def test_manifest_real_target_qemu_smoke_uses_disposable_worktree(tmp_path: Path) -> None:
    repo = _create_manifest_boot_repo(tmp_path / "target")

    result = asyncio.run(
        run_manifest_smoke(
            repo,
            "debug",
            "smoke",
            SafeProcessRunner(),
            ArtifactStore(tmp_path / "artifacts"),
            worktrees_root=tmp_path / "runtime" / "worktrees",
            lab_root=Path.cwd(),
            timeout=60,
        )
    )

    assert result["ok"] is True
    assert result["outcome"] == Outcome.PASS
    assert result["network"] == "none"
    assert '"event":"READY"' in result["serial_excerpt"]
    assert '"event":"PASS"' in result["serial_excerpt"]
    assert Path(result["build"]["build_worktree"]).is_dir()
    assert not (repo / "build").exists()
