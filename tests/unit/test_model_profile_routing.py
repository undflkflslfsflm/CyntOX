# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from oslab import model_registry
from oslab.model_registry import provider_for_role
from scripts import cyntox_cli, cyntox_council

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_model_registry_recomputes_prompt_bound_fingerprint_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_digest = "a" * 64
    registry_fingerprint, task_count = model_registry._current_mythos_benchmark_fingerprint(
        cyntox_model="cyntox:latest",
        cyntox_model_digest=model_digest,
    )
    cli_fingerprint = cyntox_cli.mythos_benchmark_fingerprint(
        cyntox_model="cyntox:latest",
        cyntox_model_digest=model_digest,
    )

    assert task_count == len(cyntox_cli.MYTHOS_BENCHMARK_TASKS)
    assert registry_fingerprint == cli_fingerprint
    assert registry_fingerprint["schema_version"] == 2
    assert model_registry._complete_mythos_benchmark_fingerprint(registry_fingerprint)

    legacy = dict(registry_fingerprint, schema_version=1)
    assert not model_registry._complete_mythos_benchmark_fingerprint(legacy)

    monkeypatch.setattr(
        model_registry,
        "canonical_prompt_sha256",
        lambda _root: "b" * 64,
    )
    drifted, _ = model_registry._current_mythos_benchmark_fingerprint(
        cyntox_model="cyntox:latest",
        cyntox_model_digest=model_digest,
    )
    assert drifted["prompt_sha256"] != registry_fingerprint["prompt_sha256"]


def test_locked_profiles_route_only_specialist_roles_away_from_cyntox() -> None:
    for role in cyntox_council.ROLE_LIBRARY:
        assert provider_for_role("single", role, root=PROJECT_ROOT) == "cyntox"
        expected = "qwythos-airllm" if role in {"fact-checker", "critic"} else "cyntox"
        assert provider_for_role("hybrid-airllm", role, root=PROJECT_ROOT) == expected
        assert provider_for_role("hybrid-airllm", role, retry=True, root=PROJECT_ROOT) == "cyntox"


def test_removed_resident_profile_fails_closed_and_is_rejected_by_council_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_profile = tmp_path / ".oslab" / "cyntox" / "model-profile.json"
    cyntox_cli.atomic_write_json(
        local_profile,
        {"model_profile": "hybrid-qwythos", "source": "manual"},
    )
    assert model_registry.configured_default_profile(tmp_path) == "single"

    monkeypatch.setattr(cyntox_council, "project_root", lambda: tmp_path)
    with pytest.raises(SystemExit) as raised:
        cyntox_council.main(
            ["--model-profile", "hybrid-qwythos", "--out-dir", "runs", "invalid route"]
        )

    assert raised.value.code == 2
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("source", [None, "typo", [], 7])
def test_persisted_hybrid_default_requires_exact_manual_source(
    tmp_path: Path, source: object
) -> None:
    local_profile = tmp_path / ".oslab" / "cyntox" / "model-profile.json"
    payload: dict[str, object] = {"model_profile": "hybrid-airllm"}
    if source is not None:
        payload["source"] = source
    cyntox_cli.atomic_write_json(local_profile, payload)

    assert model_registry.configured_default_profile(tmp_path) == "single"


def test_hybrid_dry_run_manifest_has_exact_routes_and_provider_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cyntox_council, "project_root", lambda: tmp_path)

    assert (
        cyntox_council.main(
            [
                "--dry-run",
                "--no-memory",
                "--model-profile",
                "hybrid-airllm",
                "--roles",
                "architect,fact-checker,critic,synthesizer",
                "--out-dir",
                "runs",
                "audit routing",
            ]
        )
        == 0
    )

    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert manifest["model_profile"] == "hybrid-airllm"
    assert manifest["degraded"] is False
    routes = {result["role"]: result["requested_provider"] for result in manifest["results"]}
    assert routes == {
        "architect": "cyntox",
        "fact-checker": "qwythos-airllm",
        "critic": "qwythos-airllm",
        "synthesizer": "cyntox",
    }
    required = {
        "model_profile",
        "requested_provider",
        "actual_provider",
        "model_revision",
        "fallback_reason",
        "elapsed_seconds",
        "peak_vram_mib",
    }
    assert all(required <= result.keys() for result in manifest["results"])


def test_nonzero_role_marks_manifest_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NullLease:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> None:
            pass

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(cyntox_council, "project_root", lambda: tmp_path)
    monkeypatch.setattr(cyntox_council, "GpuLease", NullLease)
    monkeypatch.setattr(cyntox_council, "gpu_free_vram_mib", lambda: 20_000.0)
    monkeypatch.setattr(
        cyntox_council,
        "run_role",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["fake"], 7, "failure", ""),
    )

    assert (
        cyntox_council.main(["--no-memory", "--roles", "architect", "--out-dir", "runs", "fail"])
        == 7
    )

    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert manifest["status"] == "failed"
    assert manifest["degraded"] is False


def test_job_wrapper_preserves_profile_provider_evidence_and_warns_when_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    job_id, job_dir, _ = cyntox_cli.create_job(
        root,
        "fallback test",
        dry_run=True,
        save_memory=False,
        model_profile="hybrid-airllm",
        skills=[],
    )

    provider = {
        "role": "fact-checker",
        "model_profile": "hybrid-airllm",
        "requested_provider": "qwythos-airllm",
        "actual_provider": "cyntox",
        "model_revision": "cyntox:latest",
        "requested_model_revision": "locked-revision",
        "fallback_reason": "worker_failure",
        "elapsed_seconds": 1.5,
        "generation_elapsed_seconds": 1.0,
        "peak_vram_mib": 2_000.0,
        "free_vram_before_mib": 20_000.0,
        "free_vram_after_mib": 19_500.0,
        "attempted_providers": [{"provider": "qwythos-airllm", "outcome": "failed"}],
    }

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[argv.index("--model-profile") + 1] == "hybrid-airllm"
        run_id = argv[argv.index("--run-id") + 1]
        out_dir = root / argv[argv.index("--out-dir") + 1] / run_id
        out_dir.mkdir(parents=True)
        output = out_dir / "synthesizer.md"
        output.write_text("answer\n", encoding="utf-8")
        (out_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "model_profile": "hybrid-airllm",
                    "status": "degraded",
                    "degraded": True,
                    "passed_threshold": True,
                    "results": [provider, {"role": "synthesizer", "output": str(output)}],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cyntox_cli.subprocess, "run", fake_run)

    assert cyntox_cli.run_job(root, job_id) == 0

    _, job = cyntox_cli.load_job(root, job_id)
    assert job["model_profile"] == "hybrid-airllm"
    assert job["degraded"] is True
    assert all(job["provider_results"][0][key] == value for key, value in provider.items())
    assert "used a provider fallback" in capsys.readouterr().err
    summary = cyntox_cli.render_job_summary(job_dir, job)
    assert "Model profile: hybrid-airllm" in summary
    assert "Degraded: True" in summary


def test_resume_and_retry_preserve_or_override_model_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *_args, **_kwargs: 1234)
    original_id, _, _ = cyntox_cli.create_job(
        root, "profile lifecycle", model_profile="hybrid-airllm", skills=[]
    )

    assert cyntox_cli.cmd_jobs(root, ["resume", original_id]) == 0
    _, resumed = cyntox_cli.load_job(root, original_id)
    assert resumed["model_profile"] == "hybrid-airllm"

    assert cyntox_cli.cmd_jobs(root, ["retry", original_id]) == 0
    assert cyntox_cli.cmd_jobs(root, ["retry", original_id, "--model-profile", "single"]) == 0
    retries = [job for job in cyntox_cli.list_jobs(root) if job.get("retry_of") == original_id]
    assert {job["model_profile"] for job in retries} == {"single", "hybrid-airllm"}


def test_resume_and_retry_migrate_retired_profile_to_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *_args, **_kwargs: 1234)
    original_id, original_dir, original = cyntox_cli.create_job(
        root, "legacy profile lifecycle", model_profile="single", skills=[]
    )
    original["model_profile"] = "hybrid-qwythos"
    cyntox_cli.save_job(original_dir, original)

    assert cyntox_cli.cmd_jobs(root, ["resume", original_id]) == 0
    _, resumed = cyntox_cli.load_job(root, original_id)
    assert resumed["model_profile"] == "single"
    assert resumed["model_profile_migrated_from"] == "hybrid-qwythos"

    resumed["state"] = "failed"
    resumed["model_profile"] = "hybrid-qwythos"
    cyntox_cli.save_job(original_dir, resumed)
    assert cyntox_cli.cmd_jobs(root, ["retry", original_id]) == 0
    retry = next(job for job in cyntox_cli.list_jobs(root) if job.get("retry_of") == original_id)
    assert retry["model_profile"] == "single"


def test_mythos_cli_forwards_explicit_model_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_benchmark(_root: Path, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "average_score": None,
            "passed_average_quality": False,
            "passed_min_task_quality": False,
            "json_report": "dry-run.json",
            "passed": False,
        }

    monkeypatch.setattr(cyntox_cli, "run_mythos_benchmark", fake_benchmark)

    assert (
        cyntox_cli.cmd_benchmark(
            tmp_path, ["mythos", "--dry-run", "--model-profile", "hybrid-airllm"]
        )
        == 0
    )
    assert captured["model_profile"] == "hybrid-airllm"


def test_legacy_job_without_model_profile_runs_as_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    job_id, job_dir, job = cyntox_cli.create_job(
        root, "legacy", dry_run=True, save_memory=False, skills=[]
    )
    job.pop("model_profile")
    cyntox_cli.save_job(job_dir, job)

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[argv.index("--model-profile") + 1] == "single"
        run_id = argv[argv.index("--run-id") + 1]
        out_dir = root / argv[argv.index("--out-dir") + 1] / run_id
        out_dir.mkdir(parents=True)
        (out_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "passed_threshold": True,
                    "results": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cyntox_cli.subprocess, "run", fake_run)

    assert cyntox_cli.run_job(root, job_id) == 0
    _, completed = cyntox_cli.load_job(root, job_id)
    assert completed["model_profile"] == "single"
