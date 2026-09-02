from __future__ import annotations

import json
from pathlib import Path

from scripts import cyntox_cli


def test_create_job_writes_required_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    job_id, job_dir, job = cyntox_cli.create_job(
        root,
        "Plan Jellyfin on the 4090 PC",
        dry_run=True,
    )

    assert job_id == job["id"]
    assert job["state"] == "queued"
    assert job["quality_target"] == 9.0
    for name in [
        "job.json",
        "prompt.md",
        "output.md",
        "commands.jsonl",
        "events.jsonl",
        "errors.log",
        "verification.md",
        "score.json",
    ]:
        assert (job_dir / name).exists()


def test_infer_skills_does_not_match_pi_inside_ping() -> None:
    assert "pc-admin" not in cyntox_cli.infer_skills("Say exactly: ping")
    assert "pc-admin" in cyntox_cli.infer_skills("check raspberry pi uptime")


def test_load_devices_and_doctor_status(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "devices.toml").write_text(
        """
        [devices.raspberry-pi]
        type = "raspberry-pi"
        host = "raspberrypi.local"
        connection = "ssh"
        configured = false
        approved_writes = false
        """,
        encoding="utf-8",
    )

    device = cyntox_cli.resolve_device(root, "raspberry-pi")
    status, issues = cyntox_cli.device_status(device)

    assert status == "needs_config"
    assert "configured=false" in issues


def test_run_on_unconfigured_device_creates_needs_input_job(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "devices.toml").write_text(
        """
        [devices.raspberry-pi]
        type = "raspberry-pi"
        host = "raspberrypi.local"
        connection = "ssh"
        configured = false
        approved_writes = false
        """,
        encoding="utf-8",
    )

    code = cyntox_cli.cmd_run_on(root, ["raspberry-pi", "check uptime"])

    jobs = cyntox_cli.list_jobs(root)
    assert code == 2
    assert len(jobs) == 1
    assert jobs[0]["state"] == "needs_input"
    assert jobs[0]["device"] == "raspberry-pi"


def test_setup_jellyfin_is_dry_run_first(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    (root / "devices.toml").write_text(
        """
        [devices.local-4090-pc]
        type = "windows-pc"
        host = "localhost"
        connection = "local"
        configured = true
        approved_writes = false
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *_args, **_kwargs: 1234)

    code = cyntox_cli.cmd_setup(root, ["jellyfin", "--target", "local-4090-pc"])

    jobs = cyntox_cli.list_jobs(root)
    assert code == 0
    assert len(jobs) == 1
    assert jobs[0]["dry_run"] is True
    assert jobs[0]["service"] == "jellyfin"
    assert "media-server" in jobs[0]["skills"]


def test_jobs_retry_clones_metadata(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *_args, **_kwargs: 1234)
    job_id, _, _ = cyntox_cli.create_job(
        root,
        "Research notes summary",
        kind="task",
        skills=["research-notes"],
        dry_run=True,
    )

    code = cyntox_cli.cmd_jobs(root, ["retry", job_id])

    jobs = cyntox_cli.list_jobs(root)
    retry_jobs = [job for job in jobs if job.get("retry_of") == job_id]
    assert code == 0
    assert retry_jobs
    assert retry_jobs[0]["skills"] == ["research-notes"]


def test_jobs_report_writes_summary(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _, job_dir, job = cyntox_cli.create_job(root, "done task", dry_run=True)
    job["state"] = "done"
    job["latest_score"] = 9.3
    cyntox_cli.save_job(job_dir, job)

    code = cyntox_cli.write_jobs_report(root, jobs_dir=cyntox_cli.DEFAULT_JOBS_DIR)

    assert code == 0
    report = json.loads((root / ".oslab" / "cyntox" / "reports" / "latest-report.json").read_text())
    assert report["total_jobs"] == 1
    assert report["average_score"] == 9.3


def test_jobs_sweep_stale_marks_dead_running_job_failed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    _, job_dir, job = cyntox_cli.create_job(root, "stale", dry_run=True)
    job["state"] = "running"
    job["pid"] = 999999
    cyntox_cli.save_job(job_dir, job)
    monkeypatch.setattr(cyntox_cli, "pid_is_running", lambda _pid: False)

    code = cyntox_cli.cmd_jobs(root, ["sweep-stale"])

    jobs = cyntox_cli.list_jobs(root)
    assert code == 0
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["failure"] == "stale_worker"
