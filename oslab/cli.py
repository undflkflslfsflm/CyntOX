from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from oslab.artifacts import ArtifactStore
from oslab.campaign import prove_recovery
from oslab.config import default_config, load_config
from oslab.database import LabDatabase
from oslab.doctor import collect_report, write_report
from oslab.eval import AgenticFixLoop, EvaluationHarness
from oslab.fuzz import replay_fixture_input, run_fixture_fuzz
from oslab.model import OllamaProvider, QwenCodeWorker
from oslab.qemu import DockerQemuBackend
from oslab.targets import inspect_targets

app = typer.Typer(no_args_is_help=True, help="Qwen OS Lab safety-bounded reliability supervisor")
model_app = typer.Typer(no_args_is_help=True, help="Probe and benchmark the local model")
integrity_app = typer.Typer(no_args_is_help=True, help="Verify database and artifact integrity")
target_app = typer.Typer(no_args_is_help=True, help="Inspect fixture and authorized real OS targets")
campaign_app = typer.Typer(no_args_is_help=True, help="Run bounded persistent campaigns")
fuzz_app = typer.Typer(no_args_is_help=True, help="Run and replay bounded fixture fuzzing")
eval_app = typer.Typer(no_args_is_help=True, help="Run seeded evaluation variants")
app.add_typer(model_app, name="model")
app.add_typer(integrity_app, name="integrity")
app.add_typer(target_app, name="target")
app.add_typer(campaign_app, name="campaign")
app.add_typer(fuzz_app, name="fuzz")
app.add_typer(eval_app, name="eval")


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
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if target != "fixture" or profile not in {"debug", "release"}:
        typer.echo("unsupported target/profile; available: fixture debug|release", err=True)
        raise typer.Exit(2)
    config = load_config()
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    result = asyncio.run(backend.build_fixture())
    _emit({"target": target, "profile": profile, **result}, json_output)


@app.command()
def boot(
    target: Annotated[str, typer.Option(help="Allowlisted target name")] = "fixture",
    seed: Annotated[int, typer.Option()] = 1,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if target != "fixture":
        typer.echo("only the discovered fixture target is available", err=True)
        raise typer.Exit(2)
    config = load_config()
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    result = asyncio.run(backend.exercise("pass", seed=seed))
    _emit(result.__dict__, json_output)
    if result.outcome.value != "PASS":
        raise typer.Exit(1)


@app.command("test")
def run_test(
    target: Annotated[str, typer.Option(help="Allowlisted target name")] = "fixture",
    test_id: Annotated[
        str, typer.Option("--test", help="pass|fail|crash|hang|snapshot|seeded")
    ] = "pass",
    seed: Annotated[int, typer.Option()] = 1,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if target != "fixture" or test_id not in {
        "pass",
        "fail",
        "crash",
        "hang",
        "snapshot",
        "seeded",
    }:
        typer.echo("unsupported fixture test", err=True)
        raise typer.Exit(2)
    config = load_config()
    backend = DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root))
    result = asyncio.run(backend.exercise(test_id, seed=seed))
    _emit(result.__dict__, json_output)
    expected = {
        "pass": "PASS",
        "fail": "FAIL",
        "crash": "CRASH",
        "hang": "HANG",
        "snapshot": "PASS",
        "seeded": "FAIL",
    }[test_id]
    if result.outcome.value != expected:
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
    output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    _emit({**result, "saved": str(output)}, json_output)


@model_app.command("qwen-code-smoke")
def qwen_code_smoke(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()

    async def run() -> dict[str, Any]:
        worker = QwenCodeWorker(config.project_root)
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
            raise RuntimeError("Qwen Code did not complete the exact controlled MCP task")
        record = ArtifactStore(config.artifacts_root).put_json(
            proof, "qwen-code-mcp-smoke.json"
        )
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
            run_fixture_fuzz(
                load_config(), campaign_id, seed=seed, total_iterations=iterations
            )
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
    base_commit: Annotated[str, typer.Option(help="Immutable source commit")] = "HEAD",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    config = load_config()
    try:
        parsed = [int(item.strip()) for item in seeds.split(",") if item.strip()]
        commit = _resolve_commit(config.project_root, base_commit)
        result = asyncio.run(EvaluationHarness(config).run(parsed, commit))
    except Exception as exc:
        typer.echo(json.dumps({"error": type(exc).__name__, "message": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    _emit(result, json_output)


@app.command("reproduce")
def reproduce(
    mode: Annotated[str, typer.Option(help="crash or seeded")] = "crash",
    cold_boots: Annotated[int, typer.Option(min=2, max=5)] = 2,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if mode not in {"crash", "seeded"}:
        typer.echo("mode must be crash or seeded", err=True)
        raise typer.Exit(2)
    config = load_config()

    async def run() -> dict[str, Any]:
        results = [
            await DockerQemuBackend(config.project_root, ArtifactStore(config.artifacts_root)).exercise(
                mode, seed=index + 1
            )
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
            "mode": mode,
            "cold_boots": cold_boots,
            "outcomes": [result.outcome for result in results],
            "fingerprints": fingerprints,
            "stable": stable,
            "artifacts": [result.artifacts for result in results],
        }
        record = ArtifactStore(config.artifacts_root).put_json(
            proof, f"reproduce-{mode}.json"
        )
        return {**proof, "artifact_sha256": record.sha256}

    result = asyncio.run(run())
    _emit(result, json_output)
    if not result["stable"]:
        raise typer.Exit(1)


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
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy", "oslab"],
    ]
    if live:
        commands.append([sys.executable, "-m", "oslab.cli", "model", "probe", "--live", "--json"])
    rows: list[dict[str, Any]] = []
    for command in commands:
        completed = __import__("subprocess").run(
            command, cwd=config.project_root, capture_output=True, text=True, check=False
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


def _resolve_commit(root: Path, reference: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    completed = subprocess.run(  # noqa: S603 - resolved git binary, no shell
        [git, "rev-parse", "--verify", f"{reference}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or f"invalid commit: {reference}")
    return completed.stdout.strip()


if __name__ == "__main__":
    app()
