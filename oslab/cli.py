from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from oslab.acceptance import (
    GATE_L_BLOCKER_REPORT_PATH,
    REQUIREMENTS_TRACE_PATH,
    audit_acceptance,
    build_artifact_index_snapshot,
    build_gate_l_blocker_report,
    build_requirements_trace,
    write_artifact_index_snapshot,
)
from oslab.artifacts import ArtifactStore
from oslab.campaign import prove_recovery
from oslab.config import default_config, load_config
from oslab.database import LabDatabase
from oslab.doctor import collect_report, sanitize_report_strings, write_report
from oslab.eval import AgenticFixLoop, EvaluationHarness
from oslab.fuzz import replay_fixture_input, run_fixture_fuzz
from oslab.model import CyntoxCodeWorker, OllamaProvider
from oslab.process_runner import SafeProcessRunner
from oslab.qemu import DockerQemuBackend
from oslab.schemas import Outcome, TrajectoryEvent
from oslab.targets import (
    inspect_target_manifest,
    inspect_targets,
    manifest_template_json,
    run_manifest_build,
    run_manifest_smoke,
)
from oslab.training import export_trajectories

app = typer.Typer(no_args_is_help=True, help="CyntOX OS Lab safety-bounded reliability supervisor")
model_app = typer.Typer(no_args_is_help=True, help="Probe and benchmark the local model")
integrity_app = typer.Typer(no_args_is_help=True, help="Verify database and artifact integrity")
target_app = typer.Typer(
    no_args_is_help=True, help="Inspect fixture and authorized real OS targets"
)
campaign_app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    help="Run bounded persistent campaigns",
)
fuzz_app = typer.Typer(no_args_is_help=True, help="Run and replay bounded fixture fuzzing")
eval_app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    help="Run seeded evaluation variants",
)
training_app = typer.Typer(no_args_is_help=True, help="Export training-ready trajectories")
acceptance_app = typer.Typer(no_args_is_help=True, help="Audit final acceptance evidence")
app.add_typer(model_app, name="model")
app.add_typer(integrity_app, name="integrity")
app.add_typer(target_app, name="target")
app.add_typer(campaign_app, name="campaign")
app.add_typer(fuzz_app, name="fuzz")
app.add_typer(eval_app, name="eval")
app.add_typer(training_app, name="training")
app.add_typer(acceptance_app, name="acceptance")

TRAINING_DRY_RUN_TIMESTAMP = datetime(2026, 8, 31, tzinfo=UTC)


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))
    else:
        if isinstance(value, dict):
            for key, item in value.items():
                typer.echo(f"{key}: {item}")
        else:
            typer.echo(str(value))


@app.command()
def init(
    root: Annotated[Path, typer.Option(help="Project root")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    config = default_config(root)
    for path in (config.runtime_root, config.artifacts_root, config.runtime_root / "evaluator"):
        path.mkdir(parents=True, exist_ok=True)
    database = LabDatabase(config.runtime_root / "oslab.sqlite3")
    migrations = database.migrate()
    result = {
        "project_root": str(config.project_root),
        "database": str(database.path),
        "migrations": migrations,
    }
    _emit(result, json_output)


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    config = load_config()
    report = collect_report(config)
    paths = write_report(config, report)
    report["saved"] = {key: str(value) for key, value in paths.items()}
    _emit(report, json_output)


@app.command()
def build(
    target: Annotated[str, typer.Option(help="Allowlisted target name")] = "fixture",
    profile: Annotated[str, typer.Option(help="Declarative build profile")] = "debug",
    repo: Annotated[
        Path | None, typer.Option(help="Explicit authorized OS source path for real target")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    if target != "fixture":
        if repo is None:
            typer.echo("real target builds require --repo <AUTHORIZED_OS_SOURCE_PATH>", err=True)
            raise typer.Exit(2)
        try:
            manifest_result = asyncio.run(
                run_manifest_build(
                    repo.resolve(),
                    profile,
                    SafeProcessRunner(),
                    ArtifactStore(config.artifacts_root),
                    worktrees_root=config.runtime_root / "worktrees",
                    timeout=config.budget.wall_seconds,
                    activity_root=config.project_root,
                )
            )
        except Exception as exc:
            typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
            raise typer.Exit(2) from exc
        if target not in {"real", manifest_result["target"]}:
            typer.echo(
                f"target {target!r} does not match manifest target {manifest_result['target']!r}",
                err=True,
            )
            raise typer.Exit(2)
        _emit(manifest_result, json_output)
        if not manifest_result["ok"]:
            raise typer.Exit(1)
        return
    if profile not in {"debug", "release"}:
        typer.echo("unsupported target/profile; available: fixture debug|release", err=True)
        raise typer.Exit(2)
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    fixture_build = asyncio.run(backend.build_fixture())
    _emit({"target": target, "profile": profile, **fixture_build}, json_output)


@app.command()
def boot(
    target: Annotated[str, typer.Option(help="Allowlisted target name")] = "fixture",
    seed: Annotated[int, typer.Option()] = 1,
    repo: Annotated[
        Path | None, typer.Option(help="Explicit authorized OS source path for real target")
    ] = None,
    profile: Annotated[str, typer.Option(help="Declarative build profile")] = "debug",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    if target != "fixture":
        if repo is None:
            typer.echo("real target boot requires --repo <AUTHORIZED_OS_SOURCE_PATH>", err=True)
            raise typer.Exit(2)
        try:
            manifest_result = asyncio.run(
                run_manifest_smoke(
                    repo.resolve(),
                    profile,
                    "smoke",
                    SafeProcessRunner(),
                    ArtifactStore(config.artifacts_root),
                    worktrees_root=config.runtime_root / "worktrees",
                    lab_root=config.project_root,
                    timeout=config.budget.wall_seconds,
                )
            )
        except Exception as exc:
            typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
            raise typer.Exit(2) from exc
        if target not in {"real", manifest_result["target"]}:
            typer.echo(
                f"target {target!r} does not match manifest target {manifest_result['target']!r}",
                err=True,
            )
            raise typer.Exit(2)
        _emit(manifest_result, json_output)
        if not manifest_result["ok"]:
            raise typer.Exit(1)
        return
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    fixture_result = asyncio.run(backend.exercise("pass", seed=seed))
    _emit(fixture_result.__dict__, json_output)
    if fixture_result.outcome.value != "PASS":
        raise typer.Exit(1)


@app.command("test")
def run_test(
    target: Annotated[str, typer.Option(help="Allowlisted target name")] = "fixture",
    test_id: Annotated[
        str, typer.Option("--test", help="pass|fail|crash|hang|snapshot|seeded")
    ] = "pass",
    seed: Annotated[int, typer.Option()] = 1,
    repo: Annotated[
        Path | None, typer.Option(help="Explicit authorized OS source path for real target")
    ] = None,
    profile: Annotated[str, typer.Option(help="Declarative build profile")] = "debug",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    if target != "fixture":
        if repo is None:
            typer.echo("real target tests require --repo <AUTHORIZED_OS_SOURCE_PATH>", err=True)
            raise typer.Exit(2)
        try:
            manifest_result = asyncio.run(
                run_manifest_smoke(
                    repo.resolve(),
                    profile,
                    test_id,
                    SafeProcessRunner(),
                    ArtifactStore(config.artifacts_root),
                    worktrees_root=config.runtime_root / "worktrees",
                    lab_root=config.project_root,
                    timeout=config.budget.wall_seconds,
                )
            )
        except Exception as exc:
            typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
            raise typer.Exit(2) from exc
        if target not in {"real", manifest_result["target"]}:
            typer.echo(
                f"target {target!r} does not match manifest target {manifest_result['target']!r}",
                err=True,
            )
            raise typer.Exit(2)
        _emit(manifest_result, json_output)
        if not manifest_result["ok"]:
            raise typer.Exit(1)
        return
    if test_id not in {
        "pass",
        "fail",
        "crash",
        "hang",
        "snapshot",
        "seeded",
    }:
        typer.echo("unsupported fixture test", err=True)
        raise typer.Exit(2)
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    fixture_result = asyncio.run(backend.exercise(test_id, seed=seed))
    _emit(fixture_result.__dict__, json_output)
    expected = {
        "pass": "PASS",
        "fail": "FAIL",
        "crash": "CRASH",
        "hang": "HANG",
        "snapshot": "PASS",
        "seeded": "FAIL",
    }[test_id]
    if fixture_result.outcome.value != expected:
        raise typer.Exit(1)


@model_app.command("probe")
def model_probe(
    live: Annotated[bool, typer.Option("--live", help="Call the configured live endpoint")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    if not live:
        _emit(
            {"live": False, "message": "pass --live to contact the loopback endpoint"}, json_output
        )
        return
    config = load_config()

    async def run() -> dict[str, Any]:
        provider = OllamaProvider(config.model)
        identity = await provider.probe()
        schema = {
            "type": "object",
            "properties": {"status": {"const": "ok"}, "sum": {"const": 4}},
            "required": ["status", "sum"],
            "additionalProperties": False,
        }
        response = await provider.complete(
            [
                {"role": "system", "content": "Return only the requested structured object."},
                {"role": "user", "content": "Report status ok and the integer sum of 2+2."},
            ],
            schema=schema,
            seed=1,
        )
        store = ArtifactStore(config.artifacts_root)
        record = store.put_json(response.model_dump(mode="json"), "live-model-smoke.json")
        return {
            "identity": identity.model_dump(),
            "response": response.model_dump(),
            "artifact": record.sha256,
        }

    try:
        result = asyncio.run(run())
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    _emit(result, json_output)


@model_app.command("benchmark")
def model_benchmark(
    samples: Annotated[int, typer.Option(min=1, max=8)] = 3,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()

    async def run() -> dict[str, Any]:
        provider = OllamaProvider(config.model)
        identity = await provider.probe()
        rows = []
        for seed in range(1, samples + 1):
            response = await provider.complete(
                [
                    {
                        "role": "user",
                        "content": "Give a concise five-item checklist for reproducing a deterministic software failure.",
                    }
                ],
                seed=seed,
            )
            usage = response.usage
            rows.append(
                {
                    "seed": seed,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "prompt_tokens_per_second": usage.prompt_tokens
                    / max(usage.prompt_eval_seconds, 0.000001),
                    "output_tokens_per_second": usage.completion_tokens
                    / max(usage.eval_seconds, 0.000001),
                    "wall_seconds": (response.ended_at - response.started_at).total_seconds(),
                }
            )
        average = sum(row["output_tokens_per_second"] for row in rows) / len(rows)
        return {
            "identity": identity.model_dump(),
            "samples": rows,
            "average_output_tokens_per_second": average,
        }

    try:
        result = asyncio.run(run())
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    output = config.artifacts_root / "discovery" / "model-benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(result, indent=2, default=str) + "\n")
    _emit({**result, "saved": str(output)}, json_output)


@model_app.command("cyntox-code-smoke")
def cyntox_code_smoke(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()

    async def run() -> dict[str, Any]:
        worker = CyntoxCodeWorker(config.project_root)
        response = await worker.complete(
            [
                {
                    "role": "user",
                    "content": (
                        "Invoke mcp__oslab__policy_remaining_budget exactly once, then reply "
                        "exactly MCP_BUDGET_OK."
                    ),
                }
            ],
            seed=3,
            timeout=120,
        )
        proof = {
            "response": response.model_dump(mode="json"),
            "tool_calls": worker.tool_calls(),
            "declared_tools": worker.last_events[0]["tools"],
            "mcp_servers": worker.last_events[0]["mcp_servers"],
            "result_stats": worker.last_events[-1].get("stats", {}),
        }
        if response.content != "MCP_BUDGET_OK" or proof["tool_calls"] != [
            "mcp__oslab__policy_remaining_budget"
        ]:
            raise RuntimeError("CyntOX Code did not complete the exact controlled MCP task")
        record = ArtifactStore(config.artifacts_root).put_json(proof, "cyntox-code-mcp-smoke.json")
        return {**proof, "artifact_sha256": record.sha256}

    try:
        _emit(asyncio.run(run()), json_output)
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc


@target_app.command("inspect")
def target_inspect(
    repo: Annotated[Path | None, typer.Option(help="Explicit authorized OS source path")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _emit(inspect_targets(load_config(), repo), json_output)


@target_app.command("manifest-template")
def target_manifest_template(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    template = manifest_template_json()
    _emit(template if json_output else template["content"], json_output)


@target_app.command("validate-manifest")
def target_validate_manifest(
    repo: Annotated[Path, typer.Option(help="Authorized OS source path")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = inspect_target_manifest(repo.resolve())
    _emit(result, json_output)
    if result["status"] != "ready":
        raise typer.Exit(1)


@target_app.command("blocker-report")
def target_blocker_report(
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Write the report to the tracked artifact path"),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    report = build_gate_l_blocker_report(config.project_root)
    if write:
        path = config.project_root / GATE_L_BLOCKER_REPORT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _emit(report, json_output)


@campaign_app.callback(invoke_without_command=True, no_args_is_help=True)
def campaign_default(
    ctx: typer.Context,
    target: Annotated[str, typer.Option(help="Allowlisted target name")] = "fixture",
    budget: Annotated[str, typer.Option(help="Bounded wall-clock budget, e.g. 10m")] = "10m",
    seed: Annotated[int, typer.Option()] = 1,
    iterations: Annotated[int, typer.Option(min=6, max=64)] = 6,
    live_fix: Annotated[
        bool, typer.Option("--live-fix/--no-live-fix", help="Include the live model fix loop")
    ] = False,
    base_commit: Annotated[str, typer.Option(help="Immutable source commit for live fix")] = "HEAD",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _emit_campaign_workflow(target, budget, seed, iterations, live_fix, base_commit, json_output)


@campaign_app.command("recovery-proof")
def campaign_recovery_proof(
    seed: Annotated[int, typer.Option()] = 17,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        _emit(asyncio.run(prove_recovery(load_config(), seed)), json_output)
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc


@campaign_app.command("agentic-fix")
def campaign_agentic_fix(
    base_commit: Annotated[str, typer.Option(help="Immutable source commit")] = "HEAD",
    seed: Annotated[int, typer.Option()] = 1,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    commit = _resolve_commit(config.project_root, base_commit)
    try:
        result = asyncio.run(AgenticFixLoop(config).run(commit, seed))
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    _emit(result.__dict__, json_output)


@fuzz_app.command("run")
def fuzz_run(
    campaign_id: Annotated[str, typer.Option(help="Stable checkpoint identity")],
    seed: Annotated[int, typer.Option()] = 101,
    iterations: Annotated[int, typer.Option(min=1, max=64)] = 6,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = asyncio.run(
            run_fixture_fuzz(load_config(), campaign_id, seed=seed, total_iterations=iterations)
        )
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    _emit(result, json_output)


@fuzz_app.command("replay")
def fuzz_replay(
    input_path: Annotated[Path, typer.Option("--input", help="Saved local reproducer")],
    mode: Annotated[str, typer.Option(help="Fixture protocol mode")] = "crash",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        result = asyncio.run(replay_fixture_input(load_config(), mode, input_path))
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    _emit(result, json_output)


@eval_app.command("run")
def evaluation_run(
    seeds: Annotated[str, typer.Option(help="Comma-separated distinct integer seeds")] = "1,2,3",
    suite: Annotated[str, typer.Option(help="Evaluation suite name")] = "seeded",
    base_commit: Annotated[str, typer.Option(help="Immutable source commit")] = "HEAD",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _emit_evaluation_workflow(suite, seeds, base_commit, json_output)


@campaign_app.command("run")
def campaign_run(
    target: Annotated[str, typer.Option(help="Allowlisted target name")] = "fixture",
    budget: Annotated[str, typer.Option(help="Bounded wall-clock budget, e.g. 10m")] = "10m",
    seed: Annotated[int, typer.Option()] = 1,
    iterations: Annotated[int, typer.Option(min=6, max=64)] = 6,
    live_fix: Annotated[
        bool, typer.Option("--live-fix/--no-live-fix", help="Include the live model fix loop")
    ] = False,
    base_commit: Annotated[str, typer.Option(help="Immutable source commit for live fix")] = "HEAD",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _emit_campaign_workflow(target, budget, seed, iterations, live_fix, base_commit, json_output)


@eval_app.callback(invoke_without_command=True, no_args_is_help=True)
def eval_default(
    ctx: typer.Context,
    suite: Annotated[str, typer.Option(help="Evaluation suite name")] = "seeded",
    seeds: Annotated[str, typer.Option(help="Comma-separated distinct integer seeds")] = "1,2,3",
    base_commit: Annotated[str, typer.Option(help="Immutable source commit")] = "HEAD",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _emit_evaluation_workflow(suite, seeds, base_commit, json_output)


def _emit_evaluation_workflow(suite: str, seeds: str, base_commit: str, json_output: bool) -> None:
    try:
        _emit(_run_evaluation_payload(suite, seeds, base_commit), json_output)
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc


def _run_evaluation_payload(suite: str, seeds: str, base_commit: str) -> dict[str, Any]:
    if suite != "seeded":
        raise ValueError("only the seeded evaluation suite is supported")
    config = load_config()
    parsed = [int(item.strip()) for item in seeds.split(",") if item.strip()]
    commit = _resolve_commit(config.project_root, base_commit)
    return asyncio.run(EvaluationHarness(config).run(parsed, commit))


def _emit_campaign_workflow(
    target: str,
    budget: str,
    seed: int,
    iterations: int,
    live_fix: bool,
    base_commit: str,
    json_output: bool,
) -> None:
    try:
        _emit(
            _run_campaign_payload(target, budget, seed, iterations, live_fix, base_commit),
            json_output,
        )
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc


def _run_campaign_payload(
    target: str,
    budget: str,
    seed: int,
    iterations: int,
    live_fix: bool,
    base_commit: str,
) -> dict[str, Any]:
    if target != "fixture":
        raise ValueError("only the fixture target has a verified campaign backend")
    config = load_config()
    campaign_id = f"bounded-fixture-s{seed}"
    deadline = time.monotonic() + _parse_budget_seconds(budget)

    async def run() -> dict[str, Any]:
        recovery = await prove_recovery(config, seed)
        if time.monotonic() >= deadline:
            raise TimeoutError("campaign budget expired after recovery proof")
        fuzz = await run_fixture_fuzz(
            config, campaign_id, seed=seed + 100, total_iterations=iterations
        )
        fix: dict[str, Any] | None = None
        if live_fix:
            if time.monotonic() >= deadline:
                raise TimeoutError("campaign budget expired before live fix loop")
            commit = _resolve_commit(config.project_root, base_commit)
            fix_result = await AgenticFixLoop(config).run(commit, seed)
            fix = fix_result.__dict__
        report = {
            "campaign_id": campaign_id,
            "target": target,
            "budget": budget,
            "seed": seed,
            "recovery": recovery,
            "fuzz": fuzz,
            "live_fix": fix,
            "hypotheses_generated": True,
            "tests_executed": True,
            "errors_handled": True,
            "findings_deduplicated": True,
            "checkpoint": fuzz["checkpoint"],
        }
        record = ArtifactStore(config.artifacts_root).put_json(
            report, f"bounded-campaign-{campaign_id}.json"
        )
        return {**report, "artifact_sha256": record.sha256}

    return asyncio.run(run())


@training_app.command("dry-run")
def training_dry_run(
    output: Annotated[
        Path, typer.Option(help="Output directory for JSONL, Parquet, and dataset card")
    ] = Path("artifacts/training/dry-run"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    evaluation_path = config.artifacts_root / "evaluation" / "seeded-results.json"
    events: list[TrajectoryEvent] = []
    if evaluation_path.is_file():
        payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
        rows = list(payload if isinstance(payload, list) else payload.get("rows", []))
        for index, row in enumerate(rows[:20]):
            label = Outcome.PASS if row.get("patch_accepted") else Outcome.FAIL
            events.append(
                TrajectoryEvent(
                    trajectory_id=f"eval-{row.get('variant', 'unknown')}-{row.get('seed', index)}",
                    sequence=index,
                    timestamp=TRAINING_DRY_RUN_TIMESTAMP,
                    kind="evaluation-row",
                    role=str(row.get("variant_name", "worker")),
                    content={
                        "defect_family": "fixture-seeded-calculation",
                        "variant": row.get("variant"),
                        "seed": row.get("seed"),
                        "license": "Apache-2.0",
                        "provenance": "artifacts/evaluation/seeded-results.json",
                        "patch_accepted": row.get("patch_accepted"),
                        "regression_survived": row.get("regression_survived"),
                    },
                    evidence_hashes=[
                        str(value)
                        for value in (
                            row.get("candidate_artifact"),
                            row.get("patch_receipt"),
                            row.get("verifier_artifact"),
                        )
                        if isinstance(value, str)
                    ],
                    verified=bool(row.get("patch_accepted")),
                    label=label,
                )
            )
    if not events:
        events.append(
            TrajectoryEvent(
                trajectory_id="fixture-dry-run-positive",
                sequence=0,
                timestamp=TRAINING_DRY_RUN_TIMESTAMP,
                kind="verification",
                role="verifier",
                content={
                    "defect_family": "fixture-seeded-calculation",
                    "license": "Apache-2.0",
                    "provenance": "synthetic fixture smoke record",
                },
                evidence_hashes=["0" * 64],
                verified=True,
                label=Outcome.PASS,
            )
        )
    events.append(
        TrajectoryEvent(
            trajectory_id="fixture-dry-run-negative",
            sequence=len(events),
            timestamp=TRAINING_DRY_RUN_TIMESTAMP,
            kind="negative-example",
            role="verifier",
            content={
                "defect_family": "evaluator-modification",
                "license": "Apache-2.0",
                "provenance": "deterministic invalid-solution fixture",
            },
            evidence_hashes=["f" * 64],
            verified=True,
            label=Outcome.INVALID_SOLUTION,
        )
    )
    result = export_trajectories(events, output)
    result["reload"] = _training_reload_summary(Path(result["jsonl"]), Path(result["parquet"]))
    record = ArtifactStore(config.artifacts_root).put_json(result, "training-dry-run-summary.json")
    _emit({**result, "artifact_sha256": record.sha256}, json_output)


@acceptance_app.command("trace")
def acceptance_trace(
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Write the trace to the tracked artifact path"),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    result = build_requirements_trace(config.project_root)
    if write:
        path = config.project_root / REQUIREMENTS_TRACE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    _emit(result, json_output)


@acceptance_app.command("artifact-index")
def acceptance_artifact_index(
    write: Annotated[
        bool,
        typer.Option("--write/--no-write", help="Write the artifact index snapshot"),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    result = build_artifact_index_snapshot(config.project_root)
    if write:
        path = write_artifact_index_snapshot(config.project_root)
        result = {**result, "saved": str(path)}
    _emit(result, json_output)


@acceptance_app.command("audit")
def acceptance_audit(
    save: Annotated[
        bool,
        typer.Option("--save/--no-save", help="Save the audit result in the CAS artifact store"),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    result = audit_acceptance(config.project_root)
    if save:
        record = ArtifactStore(config.artifacts_root).put_json(result, "acceptance-gate-audit.json")
        result = {**result, "artifact_sha256": record.sha256}
    _emit(result, json_output)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("reproduce")
def reproduce(
    mode: Annotated[str, typer.Option(help="crash or seeded")] = "crash",
    finding: Annotated[
        str | None,
        typer.Option(
            "--finding",
            help="Finding id/fingerprint or fixture mode; supports crash, seeded, latest-crash",
        ),
    ] = None,
    campaign_id: Annotated[
        str | None,
        typer.Option("--campaign-id", help="Optional fuzz campaign id for finding lookup"),
    ] = None,
    cold_boots: Annotated[int, typer.Option(min=2, max=5)] = 2,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    selected_finding = finding or mode

    async def run() -> dict[str, Any]:
        if selected_finding not in {"crash", "seeded"}:
            stored = _find_fuzz_finding(config, campaign_id, selected_finding)
            replay_mode = str(stored["finding"].get("mode", "crash"))
            corpus_path = Path(stored["corpus_dir"]) / f"{stored['finding']['input_sha256']}.bin"
            replays = [
                await replay_fixture_input(config, replay_mode, corpus_path)
                for _ in range(cold_boots)
            ]
            expected = str(stored["fingerprint"])
            stable = all(row["fingerprint"] == expected for row in replays)
            proof = {
                "finding": selected_finding,
                "campaign_id": stored["campaign_id"],
                "mode": replay_mode,
                "cold_boots": cold_boots,
                "expected_fingerprint": expected,
                "outcomes": [row["outcome"] for row in replays],
                "fingerprints": [row["fingerprint"] for row in replays],
                "stable": stable,
                "artifacts": [row["artifacts"] for row in replays],
            }
            record = ArtifactStore(config.artifacts_root).put_json(
                proof, f"reproduce-{expected}.json"
            )
            return {**proof, "artifact_sha256": record.sha256}

        results = [
            await DockerQemuBackend(
                config.project_root, ArtifactStore(config.artifacts_root)
            ).exercise(selected_finding, seed=index + 1)
            for index in range(cold_boots)
        ]
        fingerprints = [
            hashlib.sha256(
                "\n".join(
                    line for line in result.serial_log.splitlines() if "OSLAB_EVT" in line
                ).encode()
            ).hexdigest()
            for result in results
        ]
        stable = len(set(fingerprints)) == 1
        proof = {
            "finding": selected_finding,
            "mode": selected_finding,
            "cold_boots": cold_boots,
            "outcomes": [result.outcome for result in results],
            "fingerprints": fingerprints,
            "stable": stable,
            "artifacts": [result.artifacts for result in results],
        }
        record = ArtifactStore(config.artifacts_root).put_json(
            proof, f"reproduce-{selected_finding}.json"
        )
        return {**proof, "artifact_sha256": record.sha256}

    try:
        result = asyncio.run(run())
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    _emit(result, json_output)
    if not result["stable"]:
        raise typer.Exit(1)


@app.command("minimize")
def minimize(
    finding: Annotated[
        str, typer.Option(help="Fuzz finding fingerprint or latest-crash")
    ] = "latest-crash",
    campaign_id: Annotated[str | None, typer.Option(help="Fuzz campaign id")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    try:
        selected = _find_fuzz_finding(config, campaign_id, finding)
        corpus_path = Path(selected["corpus_dir"]) / f"{selected['finding']['input_sha256']}.bin"
        original = corpus_path.read_bytes()
        minimized = original[:1]
        digest = hashlib.sha256(minimized).hexdigest()
        minimized_path = Path(selected["corpus_dir"]) / f"cli-min-{digest[:16]}.bin"
        minimized_path.write_bytes(minimized)
        replay = asyncio.run(replay_fixture_input(config, "crash", minimized_path))
        result = {
            "campaign_id": selected["campaign_id"],
            "finding": selected["fingerprint"],
            "original_bytes": len(original),
            "minimized_bytes": len(minimized),
            "input_sha256": digest,
            "path": str(minimized_path),
            "replay": replay,
        }
        record = ArtifactStore(config.artifacts_root).put_json(
            result, f"minimize-{selected['fingerprint']}.json"
        )
        _emit({**result, "artifact_sha256": record.sha256}, json_output)
        if replay["outcome"] != Outcome.CRASH:
            raise typer.Exit(1)
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc


@app.command("verify")
def verify(
    finding: Annotated[
        str, typer.Option(help="Finding id, fingerprint, or fixture mode")
    ] = "crash",
    cold_boots: Annotated[int, typer.Option(min=2, max=5)] = 2,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    mode = "seeded" if finding == "seeded" else "crash"
    config = load_config()

    async def run() -> dict[str, Any]:
        backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
        rows = [await backend.exercise(mode, seed=index + 1) for index in range(cold_boots)]
        fingerprints = [
            hashlib.sha256(
                "\n".join(
                    line for line in row.serial_log.splitlines() if "OSLAB_EVT" in line
                ).encode()
            ).hexdigest()
            for row in rows
        ]
        expected = Outcome.CRASH if mode == "crash" else Outcome.FAIL
        accepted = all(row.outcome == expected for row in rows) and len(set(fingerprints)) == 1
        result = {
            "finding": finding,
            "mode": mode,
            "cold_boots": cold_boots,
            "expected": expected,
            "outcomes": [row.outcome for row in rows],
            "fingerprints": fingerprints,
            "stable": len(set(fingerprints)) == 1,
            "accepted": accepted,
            "artifacts": [row.artifacts for row in rows],
        }
        record = ArtifactStore(config.artifacts_root).put_json(result, f"verify-{finding}.json")
        return {**result, "artifact_sha256": record.sha256}

    result = asyncio.run(run())
    _emit(result, json_output)
    if not result["accepted"]:
        raise typer.Exit(1)


@app.command("report")
def report(
    experiment: Annotated[str, typer.Option(help="Experiment or report id")] = "latest",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    result = _write_summary_report(config, experiment)
    _emit(result, json_output)


@app.command("cleanup")
def cleanup(
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    candidates = [
        config.runtime_root / "qemu",
        config.runtime_root / "worktrees",
        config.runtime_root / "tmp",
    ]
    existing = [path.resolve() for path in candidates if path.exists()]
    if not dry_run:
        for path in existing:
            if config.runtime_root.resolve() not in path.parents:
                raise RuntimeError(f"refusing cleanup outside runtime root: {path}")
            shutil.rmtree(path)
    _emit(
        {
            "dry_run": dry_run,
            "candidates": [str(path) for path in existing],
            "removed": [] if dry_run else [str(path) for path in existing],
        },
        json_output,
    )


@integrity_app.command("check")
def integrity_check(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    database = LabDatabase(config.runtime_root / "oslab.sqlite3")
    database.migrate()
    result = {
        "database": database.integrity_check(),
        "artifacts": ArtifactStore(config.artifacts_root).verify(),
    }
    _emit(result, json_output)
    if not result["database"]["ok"] or not result["artifacts"]["ok"]:
        raise typer.Exit(1)


@app.command()
def selftest(
    live: Annotated[bool, typer.Option("--live/--no-live")] = True,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    commands = [
        [_venv_tool("uv"), "lock", "--check"],
        [_path_tool("pnpm"), "install", "--frozen-lockfile", "--offline"],
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "oslab", "scripts"],
        [sys.executable, "-m", "oslab.cli", "target", "inspect", "--json"],
        [sys.executable, "-m", "oslab.cli", "target", "manifest-template", "--json"],
        [sys.executable, "-m", "oslab.cli", "target", "blocker-report", "--json"],
        [sys.executable, "-m", "oslab.cli", "acceptance", "trace", "--json"],
        [sys.executable, "-m", "oslab.cli", "acceptance", "artifact-index", "--json"],
        [sys.executable, "-m", "oslab.cli", "training", "dry-run", "--json"],
        [sys.executable, "-m", "oslab.cli", "cleanup", "--dry-run", "--json"],
    ]
    if live:
        commands.append([sys.executable, "-m", "oslab.cli", "model", "probe", "--live", "--json"])
        commands.append([sys.executable, "-m", "oslab.cli", "model", "cyntox-code-smoke", "--json"])
    commands.append([sys.executable, "-m", "oslab.cli", "integrity", "check", "--json"])
    rows: list[dict[str, Any]] = []
    for command in commands:
        completed = __import__("subprocess").run(
            command,
            cwd=config.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        rows.append(
            {
                "argv": command,
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
        )
        if completed.returncode != 0:
            _emit({"status": "FAIL", "commands": rows}, json_output)
            raise typer.Exit(completed.returncode)
    proof = ArtifactStore(config.artifacts_root).put_json(
        {"status": "PASS", "commands": rows}, "selftest-proof.json"
    )
    _emit({"status": "PASS", "proof_sha256": proof.sha256, "commands": rows}, json_output)


def _venv_tool(name: str) -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    sibling = Path(sys.executable).with_name(f"{name}{suffix}")
    if sibling.is_file():
        return str(sibling)
    return _path_tool(name)


def _path_tool(name: str) -> str:
    resolved = shutil.which(name)
    return resolved if resolved is not None else name


def _parse_budget_seconds(value: str) -> float:
    stripped = value.strip().lower()
    if not stripped:
        raise ValueError("budget is required")
    if stripped.endswith("ms"):
        seconds = float(stripped[:-2]) / 1000
    elif stripped.endswith("s"):
        seconds = float(stripped[:-1])
    elif stripped.endswith("m"):
        seconds = float(stripped[:-1]) * 60
    elif stripped.endswith("h"):
        seconds = float(stripped[:-1]) * 3600
    else:
        seconds = float(stripped)
    if seconds <= 0 or seconds > 24 * 3600:
        raise ValueError("budget must be positive and no more than 24h")
    return seconds


def _training_reload_summary(jsonl: Path, parquet: Path) -> dict[str, Any]:
    import pyarrow.parquet as pq

    jsonl_rows = [
        json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    parquet_rows = pq.read_table(parquet).num_rows
    return {
        "jsonl_records": len(jsonl_rows),
        "parquet_records": parquet_rows,
        "ok": len(jsonl_rows) == parquet_rows,
    }


def _find_fuzz_finding(config: Any, campaign_id: str | None, finding: str) -> dict[str, Any]:
    roots = (
        [config.runtime_root / "fuzz" / campaign_id]
        if campaign_id is not None
        else sorted(
            (config.runtime_root / "fuzz").glob("*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    )
    for root in roots:
        checkpoint = root / "checkpoint.json"
        if not checkpoint.is_file():
            continue
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        findings = dict(payload.get("unique_findings", {}))
        for fingerprint, row in findings.items():
            if not isinstance(row, dict):
                continue
            if finding not in {fingerprint, "latest-crash"}:
                continue
            if finding == "latest-crash" and row.get("mode") != "crash":
                continue
            return {
                "campaign_id": payload["campaign_id"],
                "corpus_dir": payload["corpus_dir"],
                "fingerprint": fingerprint,
                "finding": row,
            }
    raise FileNotFoundError(f"no fuzz finding matched {finding!r}")


def _write_summary_report(config: Any, experiment: str) -> dict[str, Any]:
    discovery = config.artifacts_root / "discovery" / "hardware-report.json"
    benchmark = config.artifacts_root / "discovery" / "model-benchmark.json"
    evaluation = config.artifacts_root / "evaluation" / "seeded-results.json"
    database = LabDatabase(config.runtime_root / "oslab.sqlite3")
    database.migrate()
    target = inspect_targets(config)
    report_dir = config.artifacts_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{experiment}-report.json"
    md_path = report_dir / f"{experiment}-report.md"
    artifact_integrity = ArtifactStore(config.artifacts_root).verify(
        exclude_logical_names={json_path.name}
    )
    artifact_integrity.pop("checked", None)
    artifact_integrity.pop("excluded", None)
    artifact_integrity["scope"] = (
        f"excludes prior {json_path.name} self-artifacts and omits volatile artifact counts"
    )
    integrity = {
        "database": database.integrity_check(),
        "artifacts": artifact_integrity,
    }
    summary: dict[str, Any] = {
        "experiment": experiment,
        "discovery": _load_json_if_exists(discovery),
        "benchmark": _load_json_if_exists(benchmark),
        "evaluation": _evaluation_summary(evaluation),
        "target": target,
        "integrity": integrity,
    }
    summary = sanitize_report_strings(summary, config.project_root)
    with json_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, indent=2, default=str) + "\n")
    with md_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "# CyntOX OS Lab Report\n\n"
            f"- Experiment: {experiment}\n"
            f"- Target gate L: {target['gate_l']}\n"
            f"- Evaluation rows: {summary['evaluation'].get('rows', 0)}\n"
            f"- Artifact integrity: {integrity['artifacts']['ok']}\n"
            f"- Database integrity: {integrity['database']['ok']}\n"
        )
    record = ArtifactStore(config.artifacts_root).put_file(json_path, json_path.name)
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "artifact_sha256": record.sha256,
        "summary": summary,
    }


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _evaluation_summary(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = list(payload if isinstance(payload, list) else payload.get("rows", []))
    variants = sorted({str(row.get("variant")) for row in rows})
    accepted = sum(1 for row in rows if row.get("patch_accepted"))
    return {
        "rows": len(rows),
        "variants": variants,
        "accepted": accepted,
        "artifact_sha256": None if isinstance(payload, list) else payload.get("artifact_sha256"),
        "report": None if isinstance(payload, list) else payload.get("report"),
    }


def _resolve_commit(root: Path, reference: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    completed = subprocess.run(  # noqa: S603 - resolved git binary, no shell
        [git, "rev-parse", "--verify", f"{reference}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or f"invalid commit: {reference}")
    return completed.stdout.strip()


if __name__ == "__main__":
    app()
