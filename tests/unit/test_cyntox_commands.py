from __future__ import annotations

import pytest

from scripts.cyntox_commands import normalize_command_args


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["check", "--json"], ["doctor", "--json"]),
        (["CHECK"], ["doctor"]),
        (["test", "--quick"], ["stress", "--quick"]),
        (["fix", "--quick"], ["stress", "--fix", "--quick"]),
        (["history", "--limit", "3"], ["stress", "history", "--limit", "3"]),
        (["status"], ["jobs", "status"]),
        (["status", "--json"], ["jobs", "status", "--json"]),
        (["status", "job-123", "--json"], ["jobs", "status", "job-123", "--json"]),
        (["show", "job-123"], ["jobs", "show", "job-123"]),
        (["resume", "job-123"], ["jobs", "resume", "job-123"]),
        (["retry", "job-123"], ["jobs", "retry", "job-123"]),
        (["report", "--json"], ["jobs", "report", "--json"]),
        (["remember", "I", "prefer", "Python"], ["memory", "add", "I", "prefer", "Python"]),
        (["recall", "project", "notes"], ["memory", "search", "project", "notes"]),
        (["forget", "note-123"], ["memory", "forget", "note-123"]),
        (
            ["use", "coding", "review", "the", "code"],
            ["skills", "use", "coding", "review", "the", "code"],
        ),
        (["airllm"], ["model", "airllm", "status"]),
        (["airllm", "--json"], ["model", "airllm", "status", "--json"]),
        (["airllm", "setup", "--dry-run"], ["model", "airllm", "setup", "--dry-run"]),
        (["jobs"], ["jobs", "list"]),
        (["skills", "--json"], ["skills", "list", "--json"]),
        (["devices"], ["devices", "list"]),
        (["audit", "--limit", "3"], ["audit", "list", "--limit", "3"]),
        (["memory"], ["memory", "path"]),
        (["privacy", "--json"], ["privacy", "policy", "--json"]),
        (["security"], ["security", "policy"]),
        (["proof", "--json"], ["proof", "mythos", "--json"]),
        (["model", "airllm", "--verify"], ["model", "airllm", "status", "--verify"]),
    ],
)
def test_short_commands_expand(argv: list[str], expected: list[str]) -> None:
    original = argv.copy()
    assert normalize_command_args(argv) == expected
    assert argv == original


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["jobs", "--jobs-dir", "runs", "--json"],
            ["jobs", "--jobs-dir", "runs", "list", "--json"],
        ),
        (["jobs", "--jobs-dir=list", "--json"], ["jobs", "--jobs-dir=list", "list", "--json"]),
        (
            ["skills", "--skills-dir", "use", "--json"],
            ["skills", "--skills-dir", "use", "list", "--json"],
        ),
        (["devices", "--devices-file", "doctor"], ["devices", "--devices-file", "doctor", "list"]),
        (
            ["memory", "--vault-dir", "path", "--memory-db", "add"],
            ["memory", "--vault-dir", "path", "--memory-db", "add", "path"],
        ),
        (
            ["privacy", "--internet-mode", "off", "--allow-domain", "scan", "--json"],
            ["privacy", "--internet-mode", "off", "--allow-domain", "scan", "policy", "--json"],
        ),
        (
            ["status", "--jobs-dir", "show", "job-123"],
            ["jobs", "--jobs-dir", "show", "status", "job-123"],
        ),
        (
            ["remember", "--vault-dir", "notes", "a note"],
            ["memory", "--vault-dir", "notes", "add", "a note"],
        ),
        (
            ["use", "--skills-dir=skills dir", "coding", "review this"],
            ["skills", "--skills-dir=skills dir", "use", "coding", "review this"],
        ),
    ],
)
def test_group_options_stay_before_inserted_action(argv: list[str], expected: list[str]) -> None:
    assert normalize_command_args(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["model"],
        ["ask", "fix", "this", "--", "--help"],
        ["lab", "build", "--target", "example"],
        ["run", "--help"],
        ["make this simpler", "--dry-run"],
        ["council", "remember", "--mode", "plan"],
        ["jobs", "show", "job-123", "--json"],
        ["jobs", "--jobs-dir", "show", "list", "--json"],
        ["skills", "--skills-dir", "use", "--help"],
        ["privacy", "--allow-domain", "policy", "scan", "text"],
        ["memory", "add", "--", "--help", "status"],
        ["jobs", "typo"],
        ["jobs", "--jobs-dir"],
        ["jobs", "--jobs-dir", "--json"],
        ["jobs", "--help"],
        ["jobs", "--json", "--help"],
        ["skills", "--skills-dir", "list", "--json", "-h"],
        ["privacy", "-h"],
        ["proof", "--help"],
        ["model", "airllm", "--help"],
        ["model", "airllm", "bogus"],
        ["jobs", "--", "--help"],
        ["model", "airllm", "--", "status"],
    ],
)
def test_existing_commands_help_errors_and_task_text_are_preserved(argv: list[str]) -> None:
    assert normalize_command_args(argv) == argv


def test_aliases_preserve_literal_task_and_shell_characters() -> None:
    text = 'Do not execute $(Get-Secret); `whoami` "quoted" C:\\notes'
    assert normalize_command_args(["remember", "--", text, "--help"]) == [
        "memory",
        "add",
        "--",
        text,
        "--help",
    ]


@pytest.mark.parametrize("group", ["jobs", "skills", "devices", "audit", "privacy", "proof"])
def test_unknown_options_remain_available_for_parser_rejection(group: str) -> None:
    result = normalize_command_args([group, "--not-a-real-option"])
    assert result[0] == group
    assert result[-1] == "--not-a-real-option"


def test_normalization_is_repeatable_and_accepts_immutable_input() -> None:
    first = normalize_command_args(("skills", "--skills-dir", "list", "--json"))
    assert normalize_command_args(first) == first
