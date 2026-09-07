from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from oslab.artifacts import ArtifactStore
from oslab.config import LabConfig
from oslab.debug import normalize_log
from oslab.fuzz.engine import FuzzCampaign
from oslab.qemu import DockerQemuBackend, FixtureResult
from oslab.resource_lease import ResourceActivityLease
from oslab.schemas import Outcome, utc_now

MODES = ("pass", "fail", "crash", "hang", "seeded", "induced-infra")
SEEDS = (b"P", b"F", b"C", b"H", b"B", b"X")


async def run_fixture_fuzz(
    config: LabConfig,
    campaign_id: str,
    *,
    seed: int = 101,
    total_iterations: int = 6,
) -> dict[str, Any]:
    with ResourceActivityLease(config.project_root, "fuzz"):
        return await _run_fixture_fuzz(
            config,
            campaign_id,
            seed=seed,
            total_iterations=total_iterations,
        )


async def _run_fixture_fuzz(
    config: LabConfig,
    campaign_id: str,
    *,
    seed: int,
    total_iterations: int,
) -> dict[str, Any]:
    if total_iterations < 1 or total_iterations > 64:
        raise ValueError("fixture fuzz iterations must be between 1 and 64")
    root = config.runtime_root / "fuzz" / campaign_id
    checkpoint = root / "checkpoint.json"
    history_path = root / "observations.json"
    resumed_from = 0
    if checkpoint.is_file():
        campaign = FuzzCampaign.resume(checkpoint)
        if campaign.seed != seed or campaign.campaign_id != campaign_id:
            raise ValueError("checkpoint identity does not match requested campaign")
        resumed_from = campaign.iterations
    else:
        campaign = FuzzCampaign(campaign_id, seed, root / "corpus")
    campaign.corpus_dir.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = (
        json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
    )
    rows: list[dict[str, Any]] = []
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    while campaign.iterations < total_iterations:
        index = campaign.iterations
        generated = campaign.next_input(list(SEEDS))
        mode = MODES[index % len(MODES)]
        qemu_mode = "infra" if mode == "induced-infra" else mode
        corpus_hash = hashlib.sha256(generated).hexdigest()
        corpus_file = campaign.corpus_dir / f"{corpus_hash}.bin"
        if not corpus_file.exists():
            corpus_file.write_bytes(generated)
        result = await backend.exercise(qemu_mode, seed=_input_seed(generated))
        fingerprint = _fingerprint(mode, result)
        is_finding = result.outcome in {
            Outcome.FAIL,
            Outcome.CRASH,
            Outcome.HANG,
        }
        is_new = (
            campaign.record_finding(
                fingerprint,
                {
                    "mode": mode,
                    "outcome": result.outcome,
                    "input_sha256": corpus_hash,
                    "serial_sha256": result.artifacts["serial"],
                },
            )
            if is_finding
            else False
        )
        row = {
            "iteration": index,
            "mode": mode,
            "input_sha256": corpus_hash,
            "input_size": len(generated),
            "outcome": result.outcome,
            "fingerprint": fingerprint,
            "new_finding": is_new,
            "artifacts": result.artifacts,
            "events": [event.get("event") for event in result.events],
        }
        rows.append(row)
        history.append(row)
        campaign.checkpoint(checkpoint)
        _atomic_json(history_path, history)
    minimized = await _minimize_and_replay(backend, campaign, rows)
    coverage = {
        "kind": "fixture protocol-state coverage (not compiler instrumentation)",
        "modes": sorted(
            {row["mode"] for row in history}
            | {str(value["mode"]) for value in campaign.unique_findings.values()}
        ),
        "outcomes": sorted(
            {str(row["outcome"]) for row in history}
            | {str(value["outcome"]) for value in campaign.unique_findings.values()}
        ),
        "required_modes": list(MODES),
        "complete": campaign.iterations >= len(MODES),
    }
    if not coverage["complete"]:
        raise RuntimeError("bounded fuzz campaign did not cover all fixture protocol modes")
    report = {
        "campaign_id": campaign_id,
        "seed": seed,
        "iterations": campaign.iterations,
        "resumed_from_iteration": resumed_from,
        "unique_inputs": len(campaign.unique_inputs),
        "unique_findings": campaign.unique_findings,
        "new_rows": rows,
        "observations": history,
        "coverage": coverage,
        "minimized": minimized,
        "checkpoint": str(checkpoint),
        "completed_at": utc_now().isoformat(),
    }
    record = ArtifactStore(config.artifacts_root).put_json(
        report, f"fixture-fuzz-{campaign_id}.json"
    )
    return {**report, "artifact_sha256": record.sha256}


async def replay_fixture_input(config: LabConfig, mode: str, input_path: Path) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError("unknown fixture fuzz mode")
    data = input_path.read_bytes()
    if not data or len(data) > 65_536:
        raise ValueError("reproducer input must contain 1..65536 bytes")
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    result = await backend.exercise(
        "infra" if mode == "induced-infra" else mode, seed=_input_seed(data)
    )
    return {
        "mode": mode,
        "outcome": result.outcome,
        "fingerprint": _fingerprint(mode, result),
        "artifacts": result.artifacts,
    }


async def _minimize_and_replay(
    backend: DockerQemuBackend, campaign: FuzzCampaign, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    selected = next(
        (row for row in rows if row["mode"] == "crash" and row.get("outcome") == Outcome.CRASH),
        None,
    )
    if selected is None:
        selected_value = next(
            (value for value in campaign.unique_findings.values() if value["mode"] == "crash"),
            None,
        )
        if selected_value is None:
            raise RuntimeError("no crash finding exists for minimization")
        input_hash = str(selected_value["input_sha256"])
        expected = str(
            next(
                key
                for key, value in campaign.unique_findings.items()
                if value["input_sha256"] == input_hash and value["mode"] == "crash"
            )
        )
    else:
        input_hash = str(selected["input_sha256"])
        expected = str(selected["fingerprint"])
    original = (campaign.corpus_dir / f"{input_hash}.bin").read_bytes()
    minimized = original[:1]
    minimized_hash = hashlib.sha256(minimized).hexdigest()
    minimized_path = campaign.corpus_dir / f"min-{minimized_hash[:16]}.bin"
    minimized_path.write_bytes(minimized)
    replay = await backend.exercise("crash", seed=_input_seed(minimized))
    observed = _fingerprint("crash", replay)
    if replay.outcome != Outcome.CRASH or observed != expected:
        raise RuntimeError("minimized reproducer did not retain the stable crash fingerprint")
    return {
        "mode": "crash",
        "original_bytes": len(original),
        "minimized_bytes": len(minimized),
        "input_sha256": minimized_hash,
        "path": str(minimized_path),
        "fingerprint": observed,
        "replay_outcome": replay.outcome,
        "artifacts": replay.artifacts,
    }


def _fingerprint(mode: str, result: FixtureResult) -> str:
    event_names = sorted(
        str(event.get("event")) for event in result.events if event.get("event") not in {"READY"}
    )
    payload = {
        "mode": mode,
        "outcome": result.outcome,
        "events": event_names,
        "signal": normalize_log(
            "\n".join(
                line
                for line in result.serial_log.splitlines()
                if "OSLAB_EVT" in line and '"event":"READY"' not in line
            )
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _input_seed(value: bytes) -> int:
    return int.from_bytes(hashlib.sha256(value).digest()[:4], "big")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, sort_keys=True, default=str) + "\n")
    temporary.replace(path)
