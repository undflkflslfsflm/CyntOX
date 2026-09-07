from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import cyntox_cli


@pytest.mark.parametrize(
    ("command", "handler_name", "forwarded", "exit_code"),
    [
        (["check", "--json"], "cmd_doctor", ["--json"], 1),
        (["test", "--quick", "--skip-qemu"], "cmd_stress", ["--quick", "--skip-qemu"], 2),
        (["fix", "--quick"], "cmd_stress", ["--fix", "--quick"], 3),
        (["history", "--limit", "4"], "cmd_stress", ["history", "--limit", "4"], 4),
        (["status", "job-123", "--json"], "cmd_jobs", ["status", "job-123", "--json"], 5),
        (["show", "job-123", "--full"], "cmd_jobs", ["show", "job-123", "--full"], 6),
        (["resume", "job-123"], "cmd_jobs", ["resume", "job-123"], 7),
        (
            ["retry", "job-123", "--model-profile", "single"],
            "cmd_jobs",
            ["retry", "job-123", "--model-profile", "single"],
            8,
        ),
        (["report", "--json"], "cmd_jobs", ["report", "--json"], 9),
        (
            ["use", "research-notes", "check", "my notes", "--dry-run"],
            "cmd_skills",
            ["use", "research-notes", "check", "my notes", "--dry-run"],
            10,
        ),
        (["airllm", "status", "--json"], "cmd_model", ["airllm", "status", "--json"], 11),
        (["airllm", "setup", "--dry-run"], "cmd_model", ["airllm", "setup", "--dry-run"], 12),
    ],
)
def test_short_commands_forward_arguments_and_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    handler_name: str,
    forwarded: list[str],
    exit_code: int,
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    handler = Mock(return_value=exit_code)
    enqueue = Mock(side_effect=AssertionError("a command must not queue a task"))
    monkeypatch.setattr(cyntox_cli, handler_name, handler)
    monkeypatch.setattr(cyntox_cli, "enqueue_task", enqueue)

    assert cyntox_cli.main(command) == exit_code

    handler.assert_called_once_with(tmp_path, forwarded)
    enqueue.assert_not_called()


@pytest.mark.parametrize(
    ("command", "forwarded"),
    [
        (
            ["remember", "check", "the name is test", "--title", "run"],
            ["add", "check", "the name is test", "--title", "run"],
        ),
        (["recall", "fix", "history", "--json"], ["search", "fix", "history", "--json"]),
        (["forget", "check"], ["forget", "check"]),
    ],
)
def test_memory_shortcuts_preserve_command_looking_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    forwarded: list[str],
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    handler = Mock(return_value=13)
    monkeypatch.setattr(cyntox_cli, "cmd_memory", handler)

    assert cyntox_cli.main(command) == 13

    handler.assert_called_once_with(forwarded)


@pytest.mark.parametrize(
    "task",
    [
        ["test", "my application", "--foreground"],
        ["help", "me check this"],
        ["remember", "to run the report"],
        ["fix", "the quoted path C:\\My Project\\source.py"],
    ],
)
def test_explicit_ask_keeps_command_words_as_task_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: list[str]
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    enqueue = Mock(return_value=14)
    monkeypatch.setattr(cyntox_cli, "enqueue_task", enqueue)

    assert cyntox_cli.main(["ask", *task]) == 14

    enqueue.assert_called_once_with(tmp_path, task)


def test_unrecognized_task_text_remains_backwards_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    enqueue = Mock(return_value=15)
    monkeypatch.setattr(cyntox_cli, "enqueue_task", enqueue)
    task = ["Review", "my code", "--foreground"]

    assert cyntox_cli.main(task) == 15

    enqueue.assert_called_once_with(tmp_path, task)


@pytest.mark.parametrize(
    ("command", "handler_name", "forwarded", "takes_root"),
    [
        ("skills", "cmd_skills", ["list"], True),
        ("devices", "cmd_devices", ["list"], True),
        ("audit", "cmd_audit", ["list"], True),
        ("privacy", "cmd_privacy", ["policy"], False),
        ("memory", "cmd_memory", ["path"], False),
    ],
)
def test_bare_commands_choose_read_only_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    handler_name: str,
    forwarded: list[str],
    takes_root: bool,
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    handler = Mock(return_value=0)
    monkeypatch.setattr(cyntox_cli, handler_name, handler)

    assert cyntox_cli.main([command]) == 0

    if takes_root:
        handler.assert_called_once_with(tmp_path, forwarded)
    else:
        handler.assert_called_once_with(forwarded)


@pytest.mark.parametrize(
    ("command", "handler_name", "forwarded"),
    [
        (["help", "check"], "cmd_doctor", ["--help"]),
        (["help", "retry"], "cmd_jobs", ["retry", "--help"]),
        (["help", "jobs", "show"], "cmd_jobs", ["show", "--help"]),
        (["help", "airllm", "setup"], "cmd_model", ["airllm", "setup", "--help"]),
        (["help", "lab", "targets"], "cmd_lab", ["targets", "--help"]),
    ],
)
def test_command_help_routes_to_underlying_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    handler_name: str,
    forwarded: list[str],
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    handler = Mock(return_value=0)
    monkeypatch.setattr(cyntox_cli, handler_name, handler)

    assert cyntox_cli.main(command) == 0

    handler.assert_called_once_with(tmp_path, forwarded)


@pytest.mark.parametrize("command", [["ask"], ["retry"], ["remember"], ["airllm", "setup"]])
def test_action_help_exits_before_creating_jobs_or_installing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    create_job = Mock(side_effect=AssertionError("help must not create a job"))
    execute = Mock(side_effect=AssertionError("help must not install or launch anything"))
    monkeypatch.setattr(cyntox_cli, "create_job", create_job)
    monkeypatch.setattr(subprocess, "run", execute)

    with pytest.raises(SystemExit) as error:
        cyntox_cli.main(["help", *command])

    assert error.value.code == 0
    assert "usage:" in capsys.readouterr().out
    create_job.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [
        ["help", "please write some code"],
        ["help", "unknown", "--foreground"],
        ["help", "_worker", "job-123"],
        ["help", "_audit_worker", "audit-123"],
        ["help", "--foreground"],
        ["help", "all", "unexpected"],
        ["help", "ask", "--", "run"],
        ["help", "remember", "--", "some text"],
        ["help", "skills", "use", "research-notes", "--", "write a note"],
    ],
)
def test_invalid_help_never_queues_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    enqueue = Mock(side_effect=AssertionError("help must never queue a task"))
    worker = Mock(side_effect=AssertionError("help must never start a worker"))
    monkeypatch.setattr(cyntox_cli, "enqueue_task", enqueue)
    monkeypatch.setattr(cyntox_cli, "cmd_memory", enqueue)
    monkeypatch.setattr(cyntox_cli, "cmd_skills", enqueue)
    monkeypatch.setattr(cyntox_cli, "run_job", worker)
    monkeypatch.setattr(cyntox_cli, "_run_audit_worker", worker)

    assert cyntox_cli.main(command) == 2

    enqueue.assert_not_called()
    worker.assert_not_called()


def test_expanded_help_lists_advanced_commands_without_running_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    enqueue = Mock(side_effect=AssertionError("help must never queue a task"))
    monkeypatch.setattr(cyntox_cli, "enqueue_task", enqueue)

    assert cyntox_cli.main(["help", "all"]) == 0

    output = capsys.readouterr().out
    assert "cyntox lab" in output
    assert "cyntox benchmark" in output
    assert "cyntox audit" in output
    enqueue.assert_not_called()


@pytest.mark.parametrize("command", ["resume", "retry"])
def test_recovery_shortcuts_still_require_explicit_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    load_job = Mock(side_effect=AssertionError("missing job id must not choose a job"))
    start_worker = Mock(side_effect=AssertionError("missing job id must not start work"))
    monkeypatch.setattr(cyntox_cli, "load_job", load_job)
    monkeypatch.setattr(cyntox_cli, "start_worker", start_worker)

    with pytest.raises(SystemExit) as error:
        cyntox_cli.main([command])

    assert error.value.code == 2
    assert "job_id" in capsys.readouterr().err
    load_job.assert_not_called()
    start_worker.assert_not_called()


@pytest.mark.parametrize("single_job", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_status_shortcut_returns_parseable_job_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    single_job: bool,
    full: bool,
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    task = "Investigate the saved evidence. " * 100
    job_id, _, _ = cyntox_cli.create_job(tmp_path, task, dry_run=True)
    command = ["status", *([job_id] if single_job else []), "--json"]
    if full:
        command.append("--full")

    assert cyntox_cli.main(command) == 0

    payload = json.loads(capsys.readouterr().out)
    result = payload if single_job else payload["jobs"][0]
    assert result["id"] == job_id
    assert result["state"] == "queued"
    if full:
        assert result["task"] == task
    else:
        assert len(result["task"]) < len(task)


def test_bare_jobs_lists_saved_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    job_id, _, _ = cyntox_cli.create_job(tmp_path, "Inspect the local build", dry_run=True)

    assert cyntox_cli.main(["jobs"]) == 0

    output = capsys.readouterr().out
    assert job_id in output
    assert "queued" in output


@pytest.mark.parametrize(
    ("args", "forwarded", "exit_code"),
    [
        ([], ["--help"], 0),
        (["targets", "list"], ["targets", "list"], 0),
        (
            ["inspect", "a path with spaces; echo nope"],
            ["inspect", "a path with spaces; echo nope"],
            17,
        ),
    ],
)
def test_lab_uses_project_python_and_preserves_argument_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    forwarded: list[str],
    exit_code: int,
) -> None:
    monkeypatch.setattr(cyntox_cli, "project_root", lambda: tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=exit_code))
    monkeypatch.setattr(subprocess, "run", run)

    assert cyntox_cli.main(["lab", *args]) == exit_code

    run.assert_called_once()
    assert run.call_args.args == ([sys.executable, "-m", "oslab.cli", *forwarded],)
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert not run.call_args.kwargs.get("shell", False)
