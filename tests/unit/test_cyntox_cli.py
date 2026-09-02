from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import cyntox_cli


def write_doctor_fixture(root: Path, *, max_tokens: int = 8192, num_ctx: int = 32768) -> None:
    for name in ["cyntox.cmd", "cyntox.ps1", "qwen-code.cmd", "qwen-code.ps1"]:
        (root / name).write_text("stub\n", encoding="utf-8")
    (root / ".qwen").mkdir(parents=True)
    (root / ".qwen" / "cyntox-banner.txt").write_text("CyntOX\n", encoding="utf-8")
    package_dir = root / "node_modules" / "@qwen-code" / "qwen-code"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text('{"version":"0.22.3"}\n', encoding="utf-8")
    settings_dir = root / ".oslab" / "qwen-code-workspace" / ".qwen"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "model": {
                    "name": "cyntox",
                    "baseUrl": "http://127.0.0.1:11437/v1",
                    "generationConfig": {
                        "contextWindowSize": num_ctx,
                        "extra_body": {"options": {"num_ctx": num_ctx}},
                        "samplingParams": {"max_tokens": max_tokens},
                    },
                },
                "ui": {"mouseTracking": False, "useTerminalBuffer": False},
                "permissions": {"deny": ["display_image", "web_fetch", "web_search"]},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "^run_shell_command$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "& python scripts\\cyntox_qwen_hook.py",
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def stub_external_doctor_checks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cyntox_cli,
        "read_proxy_health",
        lambda _port: (
            {
                "ok": True,
                "service": "cyntox-openai-proxy",
                "max_tokens": 8192,
                "num_ctx": 32768,
            },
            None,
        ),
    )
    monkeypatch.setattr(
        cyntox_cli,
        "inspect_docker_qemu",
        lambda checks: cyntox_cli.add_doctor_check(checks, "docker/qemu stress", "ok", "stubbed"),
    )
    monkeypatch.setattr(
        cyntox_cli,
        "inspect_git_remote",
        lambda _root, checks: cyntox_cli.add_doctor_check(checks, "git remote", "ok", "stubbed"),
    )


def stub_process_leak_scan(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cyntox_cli,
        "process_leak_scan_result",
        lambda _root: {
            "name": "process leak scan",
            "command": ["internal", "process-leak-scan"],
            "status": "ok",
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "stdout_tail": "no suspected .oslab/pytest process leaks",
            "stderr_tail": "",
            "leaks": [],
        },
    )


def test_safe_text_capture_kwargs_uses_utf8_replacement() -> None:
    kwargs = cyntox_cli.safe_text_capture_kwargs(timeout=3)

    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["timeout"] == 3


def test_terminal_json_compacts_long_strings_and_lists() -> None:
    payload = {"long": "A" * 2000, "items": list(range(40))}

    parsed = json.loads(cyntox_cli.terminal_json(payload))

    assert parsed["_cyntox_terminal"]["compacted"] is True
    assert "terminal JSON truncated" in parsed["long"]
    assert parsed["items"][-1] == {"_truncated_items": 15}


def test_doctor_report_is_ok_with_daily_output_limits(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    write_doctor_fixture(root)
    stub_external_doctor_checks(monkeypatch)

    report = cyntox_cli.build_doctor_report(root)

    assert report["status"] == "ok"
    qwen_settings = next(check for check in report["checks"] if check["name"] == "qwen settings")
    assert qwen_settings["status"] == "ok"


def test_doctor_report_fails_when_output_limit_regresses(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    write_doctor_fixture(root, max_tokens=1024)
    stub_external_doctor_checks(monkeypatch)

    report = cyntox_cli.build_doctor_report(root)

    assert report["status"] == "fail"
    qwen_settings = next(check for check in report["checks"] if check["name"] == "qwen settings")
    assert qwen_settings["status"] == "fail"
    assert "max_tokens=1024" in qwen_settings["details"]["problems"]


def test_doctor_report_surfaces_docker_desktop_stale_socket(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    local_app_data = tmp_path / "LocalAppData"
    log = local_app_data / "Docker" / "log" / "host" / "com.docker.backend.exe.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "backend crashed, dumping error to file and reporting to user: "
        "starting services: initializing Ingest server: listening on "
        "unix://C:/Users/vikto/AppData/Local/Docker/run/sailor-ingest.sock: "
        "remove C:/Users/vikto/AppData/Local/Docker/run/sailor-ingest.sock: "
        "The file cannot be accessed by the system.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cyntox_cli.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    diagnostic = cyntox_cli.docker_desktop_recent_error()
    report = {
        "status": "warn",
        "root": str(tmp_path),
        "checks": [
            {
                "name": "docker/qemu stress",
                "status": "warn",
                "message": "Docker unavailable.",
                "details": diagnostic,
            }
        ],
    }
    rendered = cyntox_cli.render_doctor_report(report)

    assert diagnostic is not None
    assert "sailor-ingest.sock" in diagnostic["diagnostic"]
    assert str(log) == diagnostic["log"]
    assert "Diagnostic: Docker Desktop backend is crashing" in rendered
    assert "Fix: Quit Docker Desktop" in rendered


def test_stress_command_writes_report(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    stub_process_leak_scan(monkeypatch)
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "ok", "checks": [], "root": str(root)},
    )

    def fake_run_stress_command(
        fake_root: Path, name: str, command: list[str], timeout_seconds: int
    ) -> dict[str, object]:
        return {
            "name": name,
            "command": command,
            "status": "ok",
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "stdout_tail": "",
            "stderr_tail": "",
            "timeout_seconds": timeout_seconds,
            "root": str(fake_root),
        }

    monkeypatch.setattr(cyntox_cli, "run_stress_command", fake_run_stress_command)

    code = cyntox_cli.cmd_stress(
        root, ["--quick", "--skip-qemu", "--fix", "--repeat", "2", "--json"]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["status"] == "ok"
    assert output["mode"] == "quick"
    assert output["fix"] is True
    assert output["repeat"] == 2
    assert output["summary"]["repeat"] == 2
    assert output["retention"]["prune_history"] is True
    assert output["retention"]["history_count"] == 1
    assert output["commands"][0]["name"] == "format apply"
    assert output["commands"][0]["iteration"] == 1
    assert output["commands"][4]["iteration"] == 2
    assert (root / ".oslab" / "cyntox" / "reports" / "stress-latest.json").is_file()
    assert (root / ".oslab" / "cyntox" / "reports" / "stress-latest.md").is_file()
    assert Path(output["artifacts"]["history_json"]).is_file()
    assert Path(output["artifacts"]["history_markdown"]).is_file()


def test_prune_stress_history_keeps_newest_runs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    history_dir = root / ".oslab" / "cyntox" / "reports" / "stress"
    history_dir.mkdir(parents=True)
    for run_id in ("20260101-000000-aaaaaaaa", "20260102-000000-bbbbbbbb"):
        (history_dir / f"{run_id}.json").write_text("{}\n", encoding="utf-8")
        (history_dir / f"{run_id}.md").write_text("report\n", encoding="utf-8")

    removed = cyntox_cli.prune_stress_history(root, keep=1)

    assert len(removed) == 2
    assert not (history_dir / "20260101-000000-aaaaaaaa.json").exists()
    assert not (history_dir / "20260101-000000-aaaaaaaa.md").exists()
    assert (history_dir / "20260102-000000-bbbbbbbb.json").exists()
    assert (history_dir / "20260102-000000-bbbbbbbb.md").exists()


def test_stress_auto_prunes_history_after_writing_new_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    history_dir = root / ".oslab" / "cyntox" / "reports" / "stress"
    history_dir.mkdir(parents=True)
    for run_id in ("20260101-000000-aaaaaaaa", "20260102-000000-bbbbbbbb"):
        (history_dir / f"{run_id}.json").write_text("{}\n", encoding="utf-8")
        (history_dir / f"{run_id}.md").write_text("report\n", encoding="utf-8")
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "ok", "checks": [], "root": str(root)},
    )
    monkeypatch.setattr(
        cyntox_cli,
        "run_stress_command",
        lambda fake_root, name, command, timeout_seconds: {
            "name": name,
            "command": command,
            "status": "ok",
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "stdout_tail": "",
            "stderr_tail": "",
            "timeout_seconds": timeout_seconds,
            "root": str(fake_root),
        },
    )

    code = cyntox_cli.cmd_stress(root, ["--quick", "--skip-qemu", "--keep-history", "1", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["retention"]["prune_history"] is True
    assert output["retention"]["history_count"] == 1
    assert len(output["retention"]["removed"]) == 4
    assert Path(output["artifacts"]["history_json"]).is_file()
    assert len(list(history_dir.glob("*.json"))) == 1


def test_stress_no_prune_history_keeps_existing_reports(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    history_dir = root / ".oslab" / "cyntox" / "reports" / "stress"
    history_dir.mkdir(parents=True)
    (history_dir / "20260101-000000-aaaaaaaa.json").write_text("{}\n", encoding="utf-8")
    (history_dir / "20260101-000000-aaaaaaaa.md").write_text("report\n", encoding="utf-8")
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "ok", "checks": [], "root": str(root)},
    )
    monkeypatch.setattr(
        cyntox_cli,
        "run_stress_command",
        lambda fake_root, name, command, timeout_seconds: {
            "name": name,
            "command": command,
            "status": "ok",
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "stdout_tail": "",
            "stderr_tail": "",
            "timeout_seconds": timeout_seconds,
            "root": str(fake_root),
        },
    )

    code = cyntox_cli.cmd_stress(
        root, ["--quick", "--skip-qemu", "--keep-history", "1", "--no-prune-history", "--json"]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["retention"]["prune_history"] is False
    assert output["retention"]["history_count"] == 2
    assert output["retention"]["removed"] == []
    assert len(list(history_dir.glob("*.json"))) == 2


def test_stress_history_reads_newest_first(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    history_dir = root / ".oslab" / "cyntox" / "reports" / "stress"
    history_dir.mkdir(parents=True)
    old_report = {
        "id": "20260101-000000-aaaaaaaa",
        "created_at": "2026-01-01T00:00:00Z",
        "status": "ok",
        "mode": "quick",
        "repeat": 1,
        "doctor": {"status": "ok", "checks": []},
        "commands": [],
        "summary": {"flaky_commands": [], "recovered_commands": []},
    }
    new_report = {
        "id": "20260102-000000-bbbbbbbb",
        "created_at": "2026-01-02T00:00:00Z",
        "status": "warn",
        "mode": "standard",
        "repeat": 2,
        "doctor": {
            "status": "warn",
            "checks": [{"name": "git remote", "status": "warn"}],
        },
        "commands": [{"name": "qemu stress tests", "status": "warn"}],
        "summary": {"flaky_commands": [], "recovered_commands": []},
    }
    (history_dir / f"{old_report['id']}.json").write_text(json.dumps(old_report), encoding="utf-8")
    (history_dir / f"{new_report['id']}.json").write_text(json.dumps(new_report), encoding="utf-8")

    history = cyntox_cli.read_stress_history(root, limit=1)
    rendered = cyntox_cli.render_stress_history(history)

    assert history["total_history"] == 2
    assert history["shown"] == 1
    assert history["runs"][0]["id"] == new_report["id"]
    assert history["runs"][0]["warning_commands"] == ["qemu stress tests"]
    assert "doctor warnings: git remote" in rendered


def test_next_report_turns_current_warnings_into_actions(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {
            "status": "warn",
            "checks": [
                {
                    "name": "docker/qemu stress",
                    "status": "warn",
                    "message": "Docker daemon is not reachable.",
                    "details": {
                        "diagnostic": "Docker backend stale socket sailor-ingest.sock.",
                        "remediation": "Clear sailor-ingest.sock and restart Docker Desktop.",
                    },
                },
                {
                    "name": "git remote",
                    "status": "warn",
                    "message": "No git remote is configured.",
                },
            ],
        },
    )
    monkeypatch.setattr(
        cyntox_cli,
        "read_latest_stress_report",
        lambda _root: (
            {
                "status": "warn",
                "commands": [{"name": "qemu stress tests", "status": "warn"}],
                "summary": {"flaky_commands": [], "recovered_commands": []},
            },
            None,
        ),
    )
    monkeypatch.setattr(
        cyntox_cli,
        "read_stress_history",
        lambda _root, limit: {"total_history": 1, "shown": 1, "limit": limit, "runs": []},
    )

    report = cyntox_cli.build_next_report(root)
    rendered = cyntox_cli.render_next_report(report)

    assert report["status"] == "needs_action"
    assert report["actions"][0]["priority"] == "blocker"
    assert "Docker Desktop Linux engine" in rendered
    assert "Clear sailor-ingest.sock and restart Docker Desktop." in rendered
    assert "git remote add origin" in rendered
    assert "Run required QEMU proof after Docker is available" not in rendered
    assert ".\\cyntox.cmd stress history --limit 5" in rendered


def test_next_report_shows_qemu_proof_after_docker_is_ready(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "ok", "checks": []},
    )
    monkeypatch.setattr(
        cyntox_cli,
        "read_latest_stress_report",
        lambda _root: (
            {
                "status": "warn",
                "commands": [{"name": "qemu stress tests", "status": "warn"}],
                "summary": {"flaky_commands": [], "recovered_commands": []},
            },
            None,
        ),
    )
    monkeypatch.setattr(
        cyntox_cli,
        "read_stress_history",
        lambda _root, limit: {"total_history": 1, "shown": 1, "limit": limit, "runs": []},
    )

    rendered = cyntox_cli.render_next_report(cyntox_cli.build_next_report(root))

    assert "Run required QEMU proof after Docker is available" in rendered
    assert "Start Docker Desktop Linux engine" not in rendered


def test_next_report_handles_missing_stress_history(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "ok", "checks": []},
    )
    monkeypatch.setattr(
        cyntox_cli,
        "read_latest_stress_report",
        lambda _root: (None, "no stress report found yet"),
    )
    monkeypatch.setattr(
        cyntox_cli,
        "read_stress_history",
        lambda _root, limit: {"total_history": 0, "shown": 0, "limit": limit, "runs": []},
    )

    report = cyntox_cli.build_next_report(root)

    assert any(action["title"] == "Create the first stress report" for action in report["actions"])


def test_qemu_stress_skip_is_warning() -> None:
    status = cyntox_cli.stress_command_status(
        "qemu stress tests",
        0,
        "SKIPPED: QEMU e2e tests require reachable Docker daemon",
        "",
    )

    assert status == "warn"


def test_require_qemu_turns_qemu_warning_into_failure(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    stub_process_leak_scan(monkeypatch)
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "ok", "checks": [], "root": str(root)},
    )

    def fake_run_stress_command(
        _root: Path, name: str, command: list[str], _timeout_seconds: int
    ) -> dict[str, object]:
        status = "warn" if name == "qemu stress tests" else "ok"
        return {
            "name": name,
            "command": command,
            "status": status,
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "stdout_tail": "SKIPPED: QEMU e2e tests require reachable Docker daemon"
            if status == "warn"
            else "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(cyntox_cli, "run_stress_command", fake_run_stress_command)

    code = cyntox_cli.cmd_stress(root, ["--quick", "--require-qemu", "--json"])

    output = json.loads(capsys.readouterr().out)
    qemu_result = next(
        result for result in output["commands"] if result["name"] == "qemu stress tests"
    )
    assert code == 1
    assert output["status"] == "fail"
    assert output["require_qemu"] is True
    assert qemu_result["status"] == "fail"
    assert "QEMU stress was required" in qemu_result["error"]


def test_rerun_failures_classifies_recovered_failure(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    stub_process_leak_scan(monkeypatch)
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "ok", "checks": [], "root": str(root)},
    )
    attempts: dict[str, int] = {}

    def fake_run_stress_command(
        _root: Path, name: str, command: list[str], _timeout_seconds: int
    ) -> dict[str, object]:
        attempts[name] = attempts.get(name, 0) + 1
        should_fail = name == "focused cli tests" and attempts[name] == 1
        return {
            "name": name,
            "command": command,
            "status": "fail" if should_fail else "ok",
            "returncode": 1 if should_fail else 0,
            "elapsed_seconds": 0.01,
            "stdout_tail": "FAILED tests/unit/test_example.py::test_flake" if should_fail else "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(cyntox_cli, "run_stress_command", fake_run_stress_command)

    code = cyntox_cli.cmd_stress(
        root, ["--quick", "--skip-qemu", "--rerun-failures", "1", "--json"]
    )

    output = json.loads(capsys.readouterr().out)
    recovered = next(
        result for result in output["commands"] if result["name"] == "focused cli tests"
    )
    assert code == 0
    assert output["status"] == "warn"
    assert output["rerun_failures"] == 1
    assert output["summary"]["recovered_commands"] == ["focused cli tests"]
    assert recovered["status"] == "warn"
    assert recovered["recovered_on_rerun"] is True
    assert recovered["reruns"][0]["status"] == "ok"


def test_strict_stress_fails_on_warning(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    stub_process_leak_scan(monkeypatch)
    monkeypatch.setattr(
        cyntox_cli,
        "build_doctor_report",
        lambda _root: {"status": "warn", "checks": [], "root": str(root)},
    )
    monkeypatch.setattr(
        cyntox_cli,
        "run_stress_command",
        lambda _root, name, command, _timeout_seconds: {
            "name": name,
            "command": command,
            "status": "ok",
            "returncode": 0,
            "elapsed_seconds": 0.01,
            "stdout_tail": "",
            "stderr_tail": "",
        },
    )

    code = cyntox_cli.cmd_stress(root, ["--quick", "--skip-qemu", "--strict", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["status"] == "fail"
    assert output["strict"] is True


def test_stress_report_renders_warning_diagnostics() -> None:
    rendered = cyntox_cli.render_stress_report(
        {
            "status": "warn",
            "mode": "standard",
            "include_qemu": True,
            "fix": True,
            "repeat": 1,
            "root": "repo",
            "doctor": {
                "status": "warn",
                "checks": [
                    {"name": "qwen settings", "status": "ok"},
                    {
                        "name": "docker/qemu stress",
                        "status": "warn",
                        "details": {
                            "diagnostic": "Docker backend stale socket sailor-ingest.sock.",
                            "remediation": "Clear sailor-ingest.sock and restart Docker Desktop.",
                        },
                    },
                ],
            },
            "commands": [
                {
                    "name": "qemu stress tests",
                    "status": "warn",
                    "returncode": 0,
                    "elapsed_seconds": 0.1,
                    "stdout_tail": "SKIPPED: QEMU e2e tests require reachable Docker daemon",
                    "stderr_tail": "",
                }
            ],
        }
    )

    assert "Doctor warnings: docker/qemu stress" in rendered
    assert "Doctor diagnostic (docker/qemu stress): Docker backend stale socket" in rendered
    assert "Doctor fix (docker/qemu stress): Clear sailor-ingest.sock" in rendered
    assert "require reachable Docker daemon" in rendered


def test_concise_line_uses_ascii_truncation() -> None:
    line = cyntox_cli.concise_line("x" * 300, limit=20)

    assert line == ("x" * 17) + "..."


def test_powershell_single_quoted_escapes_apostrophes() -> None:
    assert cyntox_cli.powershell_single_quoted("C:/Bob's Lab") == "'C:/Bob''s Lab'"


def test_stress_flaky_commands_detect_status_changes() -> None:
    flaky = cyntox_cli.stress_flaky_commands(
        [
            {"name": "lint", "status": "ok", "iteration": 1},
            {"name": "lint", "status": "fail", "iteration": 2},
            {"name": "types", "status": "ok", "iteration": 1},
            {"name": "types", "status": "ok", "iteration": 2},
        ]
    )

    assert flaky == ["lint"]


def test_process_leaks_from_snapshot_detects_pytest_process(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pytest_child = root / ".oslab" / "pytest" / "case0"
    snapshot = [
        {
            "ProcessId": 1234,
            "ParentProcessId": 10,
            "CommandLine": f"python -c child {pytest_child}",
        },
        {
            "ProcessId": 2345,
            "ParentProcessId": 10,
            "CommandLine": f"pwsh Get-CimInstance Win32_Process {pytest_child}",
        },
        {
            "ProcessId": 5678,
            "ParentProcessId": 10,
            "CommandLine": "python -m pytest tests/unit",
        },
    ]

    leaks = cyntox_cli.process_leaks_from_snapshot(root, snapshot)

    assert [leak["pid"] for leak in leaks] == [1234]


def test_process_leak_scan_confirms_twice_before_failing(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    calls = 0

    def fake_scan(_root: Path) -> tuple[list[dict[str, object]], str | None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [{"pid": 1234, "command": "python transient"}], None
        return [], None

    monkeypatch.setattr(cyntox_cli, "scan_process_leaks", fake_scan)
    monkeypatch.setattr(cyntox_cli.time, "sleep", lambda _seconds: None)

    leaks, error, first_pass_count = cyntox_cli.confirmed_process_leaks(root)

    assert leaks == []
    assert error is None
    assert first_pass_count == 1


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


def test_jobs_list_compacts_long_task_text(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    long_task = "first line\n" + ("word " * 80)
    cyntox_cli.create_job(root, long_task, dry_run=True)

    code = cyntox_cli.cmd_jobs(root, ["list"])

    output = capsys.readouterr().out
    assert code == 0
    assert "\nword word" not in output
    assert " ..." in output
    assert len(output.splitlines()[0]) < 260


def test_jobs_show_uses_compact_human_summary(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    long_task = "first line\n" + ("taskword " * 200)
    long_output = "answer line\n" + ("outword " * 300)
    job_id, job_dir, _job = cyntox_cli.create_job(root, long_task, dry_run=True)
    (job_dir / "output.md").write_text(long_output, encoding="utf-8")

    code = cyntox_cli.cmd_jobs(root, ["show", job_id])

    output = capsys.readouterr().out
    assert code == 0
    assert output.startswith(f"CyntOX job {job_id}")
    assert "{\n" not in output
    assert "--- task preview ---" in output
    assert "--- output.md preview ---" in output
    assert "--json --full for full job metadata" in output
    assert output.count("taskword") < 200
    assert output.count("outword") < 300
    assert " ..." in output


def test_jobs_show_json_is_compact_but_valid(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    long_task = "taskword " * 500
    job_id, _job_dir, _job = cyntox_cli.create_job(root, long_task, dry_run=True)

    code = cyntox_cli.cmd_jobs(root, ["show", job_id, "--json"])

    parsed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert parsed["id"] == job_id
    assert parsed["task"] != long_task
    assert "terminal JSON truncated" in parsed["task"]
    assert parsed["_cyntox_terminal"]["compacted"] is True


def test_jobs_show_json_full_keeps_full_metadata(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    long_task = "taskword " * 500
    job_id, _job_dir, _job = cyntox_cli.create_job(root, long_task, dry_run=True)

    code = cyntox_cli.cmd_jobs(root, ["show", job_id, "--json", "--full"])

    parsed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert parsed["id"] == job_id
    assert parsed["task"] == long_task
    assert "_cyntox_terminal" not in parsed


def test_infer_skills_does_not_match_pi_inside_ping() -> None:
    assert "pc-admin" not in cyntox_cli.infer_skills("Say exactly: ping")
    assert "pc-admin" in cyntox_cli.infer_skills("check raspberry pi uptime")


def test_denied_patterns_do_not_block_harmless_formatting() -> None:
    assert cyntox_cli.task_has_denied_pattern("format the answer as JSON") is None
    assert cyntox_cli.task_has_denied_pattern("format this report as a table") is None
    assert cyntox_cli.task_has_denied_pattern("format C:") is not None
    assert cyntox_cli.task_has_denied_pattern("mkfs.ext4 /dev/sda1") is not None


def test_denied_patterns_hard_apply_only_to_execution_surfaces() -> None:
    plan_job = {"kind": "task", "mode": "plan", "task": "stress test public IPs"}
    implement_job = {"kind": "task", "mode": "implement", "task": "stress test public IPs"}
    device_job = {"kind": "run-on", "mode": "plan", "task": "stress test public IPs"}

    assert cyntox_cli.task_has_denied_pattern(str(plan_job["task"])) is not None
    assert cyntox_cli.job_uses_execution_surface(plan_job) is False
    assert cyntox_cli.job_uses_execution_surface(implement_job) is True
    assert cyntox_cli.job_uses_execution_surface(device_job) is True


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


def test_configured_device_status_requires_command_policies() -> None:
    status, issues = cyntox_cli.device_status(
        {"connection": "local", "configured": True, "host": "localhost"}
    )

    assert status == "limited"
    assert "allowed_commands-missing" in issues
    assert "denied_commands-missing" in issues


def test_devices_show_uses_compact_human_summary(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    notes = "note " * 200
    (root / "devices.toml").write_text(
        f"""
        [devices.raspberry-pi]
        name = "Raspberry Pi"
        type = "raspberry-pi"
        host = "raspberrypi.local"
        connection = "ssh"
        configured = false
        approved_writes = false
        allowed_commands = ["uptime", "df -h"]
        denied_commands = ["format", "mkfs"]
        notes = "{notes}"
        """,
        encoding="utf-8",
    )

    code = cyntox_cli.cmd_devices(root, ["show", "raspberry-pi"])

    output = capsys.readouterr().out
    assert code == 0
    assert output.startswith("CyntOX device raspberry-pi")
    assert "{\n" not in output
    assert "Status: needs_config" in output
    assert "Allowed Commands: uptime, df -h" in output
    assert "Denied Commands: format, mkfs" in output
    assert output.count("note") < 200
    assert "--json --full for full device metadata" in output


def test_devices_show_json_is_compact_but_valid(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    notes = "note " * 400
    (root / "devices.toml").write_text(
        f"""
        [devices.raspberry-pi]
        name = "Raspberry Pi"
        type = "raspberry-pi"
        host = "raspberrypi.local"
        connection = "ssh"
        configured = false
        approved_writes = false
        notes = "{notes}"
        """,
        encoding="utf-8",
    )

    code = cyntox_cli.cmd_devices(root, ["show", "raspberry-pi", "--json"])

    parsed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert parsed["name"] == "raspberry-pi"
    assert parsed["device"]["notes"] != notes
    assert "terminal JSON truncated" in parsed["device"]["notes"]
    assert parsed["_cyntox_terminal"]["compacted"] is True


def test_devices_show_json_full_keeps_full_metadata(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    notes = "note " * 400
    (root / "devices.toml").write_text(
        f"""
        [devices.raspberry-pi]
        name = "Raspberry Pi"
        type = "raspberry-pi"
        host = "raspberrypi.local"
        connection = "ssh"
        configured = false
        approved_writes = false
        notes = "{notes}"
        """,
        encoding="utf-8",
    )

    code = cyntox_cli.cmd_devices(root, ["show", "raspberry-pi", "--json", "--full"])

    parsed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert parsed["name"] == "raspberry-pi"
    assert parsed["device"]["notes"] == notes
    assert "_cyntox_terminal" not in parsed


def test_device_executor_skips_denied_commands(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    calls: list[str] = []

    def fake_run_local(_root: Path, command: str) -> object:
        calls.append(command)
        raise AssertionError("denied command should not execute")

    monkeypatch.setattr(cyntox_cli, "run_local_readonly_command", fake_run_local)
    output = cyntox_cli.execute_readonly_device_commands(
        root,
        {"connection": "local", "denied_commands": ["format"]},
        ["format disk"],
    )

    assert calls == []
    assert "SKIPPED denied command policy 'format': format disk" in output


def test_device_denied_command_match_avoids_substring_false_positive() -> None:
    device = {"denied_commands": ["dd", "format"]}

    assert cyntox_cli.device_denied_command_match(device, "add a note") is None
    assert cyntox_cli.device_denied_command_match(device, "Format-Table Name") is None
    assert cyntox_cli.device_denied_command_match(device, "dd if=/dev/zero") == "dd"
    assert cyntox_cli.device_denied_command_match(device, "format disk") == "format"


def test_device_executor_allows_policy_mapped_readonly_command(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    calls: list[str] = []

    def fake_run_local(_root: Path, command: str) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(["fake"], 0, "booted yesterday", "")

    monkeypatch.setattr(cyntox_cli, "run_local_readonly_command", fake_run_local)
    command = "Get-CimInstance Win32_OperatingSystem | Select-Object LastBootUpTime"
    output = cyntox_cli.execute_readonly_device_commands(
        root,
        {"connection": "local", "allowed_commands": ["uptime"], "denied_commands": []},
        [command],
    )

    assert calls == [command]
    assert "booted yesterday" in output


def test_device_executor_skips_unallowed_commands(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    calls: list[str] = []

    def fake_run_local(_root: Path, command: str) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(["fake"], 0, "", "")

    monkeypatch.setattr(cyntox_cli, "run_local_readonly_command", fake_run_local)
    output = cyntox_cli.execute_readonly_device_commands(
        root,
        {"connection": "local", "allowed_commands": ["uptime"], "denied_commands": []},
        ["hostnamectl"],
    )

    assert calls == []
    assert "SKIPPED not in allowed command policy: hostnamectl" in output


def test_device_executor_requires_explicit_allowed_commands(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    calls: list[str] = []

    def fake_run_local(_root: Path, command: str) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(["fake"], 0, "", "")

    monkeypatch.setattr(cyntox_cli, "run_local_readonly_command", fake_run_local)
    output = cyntox_cli.execute_readonly_device_commands(
        root,
        {"connection": "local", "denied_commands": []},
        ["uptime"],
    )

    assert calls == []
    assert "SKIPPED not in allowed command policy: uptime" in output


def test_device_status_flags_unsafe_ssh_target() -> None:
    status, issues = cyntox_cli.device_status(
        {
            "connection": "ssh",
            "configured": True,
            "host": "-oProxyCommand=bad",
            "user": "pi user",
        }
    )

    assert status == "limited"
    assert "ssh-host-starts-with-dash" in issues
    assert "ssh-user-contains-whitespace" in issues


def test_ssh_command_rejects_unsafe_target_before_subprocess(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(cyntox_cli.shutil, "which", lambda _name: "ssh")

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        raise AssertionError("unsafe ssh target should not execute")

    monkeypatch.setattr(cyntox_cli.subprocess, "run", fake_run)

    try:
        cyntox_cli.run_ssh_readonly_command(
            root,
            {"connection": "ssh", "host": "-oProxyCommand=bad", "user": "pi"},
            "uptime",
        )
    except ValueError as error:
        assert "ssh-host-starts-with-dash" in str(error)
    else:
        raise AssertionError("Expected unsafe SSH target to fail")
    assert calls == []


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


def test_foreground_task_prints_job_output(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()

    def fake_run_job(
        fake_root: Path, job_id: str, jobs_dir: str = cyntox_cli.DEFAULT_JOBS_DIR
    ) -> int:
        job_dir = cyntox_cli.jobs_root(fake_root, jobs_dir) / job_id
        (job_dir / "output.md").write_text("hello from job\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cyntox_cli, "run_job", fake_run_job)

    code = cyntox_cli.enqueue_task(root, ["--foreground", "say", "hello"])

    output = capsys.readouterr().out
    assert code == 0
    assert "hello from job" in output
    assert "finished with code 0" in output


def test_foreground_task_bounds_large_job_output(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    long_output = "B" * 80

    def fake_run_job(
        fake_root: Path, job_id: str, jobs_dir: str = cyntox_cli.DEFAULT_JOBS_DIR
    ) -> int:
        job_dir = cyntox_cli.jobs_root(fake_root, jobs_dir) / job_id
        (job_dir / "output.md").write_text(long_output + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cyntox_cli, "run_job", fake_run_job)
    monkeypatch.setenv("CYNTOX_FOREGROUND_OUTPUT_LIMIT", "12")

    code = cyntox_cli.enqueue_task(root, ["--foreground", "say", "a lot"])

    output = capsys.readouterr().out
    assert code == 0
    assert "terminal preview truncated 68 chars" in output
    assert "full output saved to" in output
    assert long_output not in output
    assert "finished with code 0" in output


def test_main_help_lists_daily_commands(capsys) -> None:  # type: ignore[no-untyped-def]
    code = cyntox_cli.main(["--help"])

    output = capsys.readouterr().out
    assert code == 0
    assert "CyntOX daily-use commands" in output
    assert "cyntox chat" in output
    assert "cyntox jobs list|show|resume|retry" in output
    assert "cyntox run-on <device>" in output


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
        allowed_services = ["jellyfin"]
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


def test_setup_rejects_service_not_allowed_for_target(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    (root / "devices.toml").write_text(
        """
        [devices.raspberry-pi]
        type = "raspberry-pi"
        host = "raspberrypi.local"
        connection = "ssh"
        configured = true
        approved_writes = false
        allowed_services = ["jellyfin-client-helper"]
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *_args, **_kwargs: 1234)

    code = cyntox_cli.cmd_setup(root, ["jellyfin", "--target", "raspberry-pi"])

    jobs = cyntox_cli.list_jobs(root)
    output = capsys.readouterr().out
    assert code == 2
    assert len(jobs) == 1
    assert jobs[0]["state"] == "needs_input"
    assert jobs[0]["dry_run"] is True
    assert "not allowed for target raspberry-pi" in output
    assert "jellyfin-client-helper" in output


def test_worker_rejects_disallowed_service_even_if_job_exists(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    (root / "devices.toml").write_text(
        """
        [devices.raspberry-pi]
        type = "raspberry-pi"
        host = "raspberrypi.local"
        connection = "ssh"
        configured = true
        approved_writes = false
        allowed_services = ["jellyfin-client-helper"]
        """,
        encoding="utf-8",
    )
    council_calls: list[list[str]] = []
    monkeypatch.setattr(
        cyntox_cli.subprocess,
        "run",
        lambda argv, **_kwargs: council_calls.append(list(argv))
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    job_id, _job_dir, _job = cyntox_cli.create_job(
        root,
        "Set up jellyfin on raspberry-pi",
        kind="setup",
        device="raspberry-pi",
        service="jellyfin",
        dry_run=True,
    )

    code = cyntox_cli.run_job(root, job_id)

    job_dir, job = cyntox_cli.load_job(root, job_id)
    assert code == 2
    assert job["state"] == "needs_input"
    assert "not allowed for target raspberry-pi" in job["needs_input_reason"]
    assert "No setup planning or execution was run" in (job_dir / "verification.md").read_text(
        encoding="utf-8"
    )
    assert council_calls == []


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


def test_jobs_sweep_stale_preserves_worker_log_tails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "repo"
    root.mkdir()
    _, job_dir, job = cyntox_cli.create_job(root, "stale with logs", dry_run=True)
    job["state"] = "running"
    job["worker_pid"] = 424242
    cyntox_cli.save_job(job_dir, job)
    stdout = "stdout-start\n" + ("x" * (cyntox_cli.WORKER_LOG_TAIL_LIMIT + 100))
    stderr = "stderr-start\n" + ("y" * (cyntox_cli.WORKER_LOG_TAIL_LIMIT + 100))
    (job_dir / "worker.stdout.log").write_text(stdout, encoding="utf-8")
    (job_dir / "worker.stderr.log").write_text(stderr, encoding="utf-8")
    monkeypatch.setattr(cyntox_cli, "pid_is_running", lambda _pid: False)

    code = cyntox_cli.cmd_jobs(root, ["sweep-stale"])

    jobs = cyntox_cli.list_jobs(root)
    assert code == 0
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["failure"] == "stale_worker"
    assert jobs[0]["stale_worker_pid"] == 424242
    assert len(jobs[0]["worker_stdout_tail"]) == cyntox_cli.WORKER_LOG_TAIL_LIMIT
    assert len(jobs[0]["worker_stderr_tail"]) == cyntox_cli.WORKER_LOG_TAIL_LIMIT
    assert jobs[0]["worker_stdout_tail"].endswith("x" * cyntox_cli.WORKER_LOG_TAIL_LIMIT)
    assert jobs[0]["worker_stderr_tail"].endswith("y" * cyntox_cli.WORKER_LOG_TAIL_LIMIT)
    verification = (job_dir / "verification.md").read_text(encoding="utf-8")
    errors = (job_dir / "errors.log").read_text(encoding="utf-8")
    output = (job_dir / "output.md").read_text(encoding="utf-8")
    assert "Stale worker sweep" in verification
    assert "worker.stdout.log tail" in verification
    assert "worker.stderr.log tail" in verification
    assert "[stale worker stderr tail]" in errors
    assert "cyntox jobs retry" in output
