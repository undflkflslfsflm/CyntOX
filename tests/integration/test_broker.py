from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from oslab.artifacts import ArtifactStore
from oslab.config import default_config
from oslab.database import LabDatabase
from oslab.process_runner import SafeProcessRunner
from oslab.schemas import Outcome
from oslab.tools import CapabilityBroker, ToolContext


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git
    result = subprocess.run(  # noqa: S603 - test arguments are fixed locally
        [git, *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init")
    (root / "value.txt").write_text("before\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@invalid",
        "commit",
        "-m",
        "base",
    )
    return root, _git(root, "rev-parse", "HEAD")


def test_transactional_patch_and_hash_mismatch_rollback(
    source_repo: tuple[Path, str], tmp_path: Path
) -> None:
    source, commit = source_repo
    config = default_config(source)
    config.runtime_root = tmp_path / "runtime"
    config.artifacts_root = tmp_path / "artifacts"
    config.allowed_roots = [source, config.runtime_root]
    context = ToolContext(
        config,
        LabDatabase(config.runtime_root / "lab.sqlite3"),
        ArtifactStore(config.artifacts_root),
        SafeProcessRunner(),
        "run-test",
        20,
    )
    broker = CapabilityBroker(context)
    created = asyncio.run(
        broker.invoke("repo.create_worktree", {"source": str(source), "commit": commit})
    )
    assert created.status == Outcome.PASS
    worktree = Path(created.data["path"])
    before = (worktree / "value.txt").read_bytes()
    good = asyncio.run(
        broker.invoke(
            "repo.apply_patch",
            {
                "worktree": str(worktree),
                "base_commit": commit,
                "changes": [
                    {
                        "path": "value.txt",
                        "expected_sha256": hashlib.sha256(before).hexdigest(),
                        "content": "after\n",
                    }
                ],
            },
        )
    )
    assert good.status == Outcome.PASS
    assert (worktree / "value.txt").read_text(encoding="utf-8") == "after\n"
    rejected = asyncio.run(
        broker.invoke(
            "repo.apply_patch",
            {
                "worktree": str(worktree),
                "base_commit": commit,
                "changes": [
                    {"path": "value.txt", "expected_sha256": "0" * 64, "content": "tamper\n"}
                ],
            },
        )
    )
    assert rejected.status == Outcome.POLICY_DENIED
    assert (worktree / "value.txt").read_text(encoding="utf-8") == "after\n"
