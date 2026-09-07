# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from oslab.artifacts import ArtifactStore
from oslab.debug import CrashEvidence, fingerprint_crash
from oslab.qemu import DockerQemuBackend
from oslab.schemas import Outcome


@pytest.mark.qemu
def test_true_qemu_fixture_modes_snapshot_and_fingerprint() -> None:
    root = Path.cwd()

    async def run() -> None:
        backend = DockerQemuBackend(root, ArtifactStore(root / "artifacts"))
        expected = {
            "pass": Outcome.PASS,
            "fail": Outcome.FAIL,
            "crash": Outcome.CRASH,
            "hang": Outcome.HANG,
            "snapshot": Outcome.PASS,
            "infra": Outcome.INFRA_ERROR,
        }
        results = {}
        for mode, outcome in expected.items():
            result = await backend.exercise(mode, seed=1)
            assert result.outcome == outcome
            assert result.artifacts["serial"]
            results[mode] = result
        assert results["snapshot"].details["snapshot_state_replayed"] is True
        fingerprints = []
        reproducer = hashlib.sha256(b"serial-command:C").hexdigest()
        for cold_run in range(3):
            crash = await backend.exercise("crash", seed=cold_run + 1)
            assert crash.outcome == Outcome.CRASH
            fingerprints.append(
                fingerprint_crash(
                    CrashEvidence(
                        "CRASH",
                        "fixture/serial",
                        "controlled invalid opcode",
                        ("boot.asm:crash",),
                        crash.events[0]["build_id"],
                        reproducer,
                    )
                )
            )
        assert len(set(fingerprints)) == 1

    asyncio.run(run())
