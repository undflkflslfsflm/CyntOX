"""Expand short public commands without interpreting or rewriting task text."""

from __future__ import annotations

from collections.abc import Sequence

_GROUP_OPTIONS: dict[str, frozenset[str]] = {
    "jobs": frozenset({"--jobs-dir"}),
    "skills": frozenset({"--skills-dir"}),
    "devices": frozenset({"--devices-file"}),
    "memory": frozenset({"--vault-dir", "--memory-db"}),
    "privacy": frozenset({"--internet-mode", "--allow-domain"}),
    "security": frozenset({"--internet-mode", "--allow-domain"}),
}
_DEFAULT_ACTIONS = {
    "jobs": "list",
    "skills": "list",
    "devices": "list",
    "audit": "list",
    "memory": "path",
    "privacy": "policy",
    "security": "policy",
    "proof": "mythos",
}
_SIMPLE_ALIASES: dict[str, tuple[str, ...]] = {
    "check": ("doctor",),
    "test": ("stress",),
    "fix": ("stress", "--fix"),
    "history": ("stress", "history"),
    "airllm": ("model", "airllm"),
}
_ACTION_ALIASES = {
    "status": ("jobs", "status"),
    "show": ("jobs", "show"),
    "resume": ("jobs", "resume"),
    "retry": ("jobs", "retry"),
    "report": ("jobs", "report"),
    "remember": ("memory", "add"),
    "recall": ("memory", "search"),
    "forget": ("memory", "forget"),
    "use": ("skills", "use"),
}
COMMAND_ALIASES = frozenset(_SIMPLE_ALIASES) | frozenset(_ACTION_ALIASES)


def _group_option_end(args: list[str], options: frozenset[str]) -> int:
    """Skip only a leading sequence of known, value-bearing group options."""
    index = 0
    while index < len(args):
        name, separator, _ = args[index].partition("=")
        if name not in options:
            break
        if separator:
            index += 1
        elif index + 1 < len(args) and not args[index + 1].startswith("-"):
            index += 2
        else:
            # Keep invalid/missing option values intact for argparse to reject.
            break
    return index


def _with_default_action(
    args: list[str], action: str, options: frozenset[str] = frozenset()
) -> list[str]:
    index = _group_option_end(args, options)
    remaining = args[index:]
    if remaining:
        first = remaining[0]
        if first == "--" or not first.startswith("-"):
            return args
        for token in remaining:
            if token == "--":
                break
            if token in {"-h", "--help"}:
                return args
        if first.partition("=")[0] in options:
            # A group option left unconsumed has no valid value.
            return args
    return [*args[:index], action, *remaining]


def normalize_command_args(argv: Sequence[str]) -> list[str]:
    """Return argv with public shortcuts expanded into established commands.

    Quoting belongs to the caller's shell: arguments are never joined, split,
    evaluated, or scanned as task text. Known group options retain their original
    position ahead of the inserted action. Existing explicit subcommands and
    invalid input remain the responsibility of their original argument parsers.
    """
    args = list(argv)
    if not args:
        return args
    command = args[0].lower()
    tail = args[1:]
    if command in _ACTION_ALIASES:
        group, action = _ACTION_ALIASES[command]
        index = _group_option_end(tail, _GROUP_OPTIONS.get(group, frozenset()))
        return [group, *tail[:index], action, *tail[index:]]
    if command in _SIMPLE_ALIASES:
        args = [*_SIMPLE_ALIASES[command], *tail]
        command, tail = args[0], args[1:]
    if command in _DEFAULT_ACTIONS:
        return [
            args[0],
            *_with_default_action(
                tail, _DEFAULT_ACTIONS[command], _GROUP_OPTIONS.get(command, frozenset())
            ),
        ]
    if command == "model" and tail and tail[0] == "airllm":
        return [args[0], "airllm", *_with_default_action(tail[1:], "status")]
    return args
