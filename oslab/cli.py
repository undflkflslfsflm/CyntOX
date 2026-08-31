from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from oslab.artifacts import ArtifactStore
from oslab.config import default_config, load_config
from oslab.database import LabDatabase
from oslab.doctor import collect_report, write_report
from oslab.model import OllamaProvider
from oslab.qemu import DockerQemuBackend

app = typer.Typer(no_args_is_help=True, help="Qwen OS Lab safety-bounded reliability supervisor")
model_app = typer.Typer(no_args_is_help=True, help="Probe and benchmark the local model")
integrity_app = typer.Typer(no_args_is_help=True, help="Verify database and artifact integrity")
app.add_typer(model_app, name="model")
app.add_typer(integrity_app, name="integrity")


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


if __name__ == "__main__":
    app()
