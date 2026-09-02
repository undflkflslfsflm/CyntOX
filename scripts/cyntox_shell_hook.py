from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

try:
    from scripts import cyntox_privacy
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    import cyntox_privacy  # type: ignore[import-not-found,no-redef]


SHELL_TOOL_NAMES = {"run_shell_command", "shell"}
NETWORK_COMMAND_RE = re.compile(
    r"\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|scp|sftp|ftp|rsync)\b"
    r"|\bgit\s+(clone|fetch|pull|submodule)\b"
    r"|\b(pip|uv|npm|pnpm|yarn|cargo|go)\s+.*\b(install|get|add)\b"
    r"|\b(winget|choco|scoop|docker)\s+.*\b(install|pull|run)\b",
    re.IGNORECASE | re.DOTALL,
)
SECRET_WORD_RE = re.compile(
    r"\b(api[_ -]?key|token|password|secret|private key|\.env|vault|system prompt|developer message)\b",
    re.IGNORECASE,
)
OUTPUT_BOUND_RE = re.compile(
    r"(--stat|--shortstat|--name-only|--name-status|--numstat|--summary|--check|--quiet)"
    r"|(\bgit\s+log\b[^\n\r|;]*(?:^|[\s])-n\s*=?\s*\d+\b)"
    r"|((?:^|[\s])(?:-m|--max-count)\s*=?\s*\d+\b)"
    r"|((?:^|[\s])-(?:TotalCount|Tail|First|Last)\s+\d+\b)"
    r"|(\bSelect-Object\b.{0,80}(?:^|[\s])-(?:First|Last)\s+\d+\b)"
    r"|(\b(?:Out-File|Set-Content|Add-Content|Export-Clixml|Export-Csv)\b)"
    r"|([^\d]>{1,2}\s*[A-Za-z0-9_.\\/: -]+)",
    re.IGNORECASE | re.DOTALL,
)
RG_COMMAND_RE = re.compile(r"\brg(?:\.exe)?\b", re.IGNORECASE)
RG_SINGLE_FILE_SCOPE_RE = re.compile(
    r"\brg(?:\.exe)?\b[^\n\r|;]*\s"
    r"(?:[A-Za-z]:)?[A-Za-z0-9_.()\\/: -]+"
    r"\.(?:py|ps1|psm1|cmd|bat|md|txt|json|jsonl|toml|ya?ml|ini|cfg|sh|ts|tsx|js|jsx|css|html)"
    r"(?:\s|$)",
    re.IGNORECASE,
)
UNBOUNDED_OUTPUT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bgit\s+diff\b", re.IGNORECASE),
        "Use `git diff --stat`, `git diff --name-only`, a path-scoped diff, or redirect the full diff to a file.",
    ),
    (
        re.compile(r"\bgit\s+show\b", re.IGNORECASE),
        "Use `git show --stat`, `git show --name-only`, or redirect the full output to a file.",
    ),
    (
        re.compile(r"\bgit\s+log\b", re.IGNORECASE),
        "Use `git log --oneline -n 30` or another explicit max-count.",
    ),
    (
        re.compile(
            r"\b(?:Get-ChildItem|gci|dir|ls)\b[^\n\r|;]*(?:^|[\s])-(?:Recurse)\b",
            re.IGNORECASE,
        ),
        "Use `-Depth`, pipe to `Select-Object -First <n>`, or redirect full recursive listings to a file.",
    ),
    (
        re.compile(r"\b(?:Get-Content|gc|type|cat)\b[^\n\r|;]*(?:\*|-Raw|\blog\b)", re.IGNORECASE),
        "Use `Get-Content -TotalCount <n>`, `Get-Content -Tail <n>`, or the text-file read tool for bounded reads.",
    ),
    (
        re.compile(r"\brg\s+(?:--files\s+)?[\"']?\.[\"']?(?:\s|$)", re.IGNORECASE),
        "Use a specific pattern/path or add a bounded preview instead of dumping the whole tree.",
    ),
    (
        re.compile(
            r"\b(?:python(?:\.exe)?\s+-m\s+oslab\.cli|oslab(?:\.exe)?|uv\s+run\s+oslab)\b"
            r"[^\n\r|;]*--json\b",
            re.IGNORECASE,
        ),
        "Redirect raw OS-lab JSON to a file, request a compact CyntOX command instead, or pipe to a bounded preview.",
    ),
    (
        re.compile(
            r"\bcyntox(?:\.cmd|\.ps1)?\b[^\n\r|;]*--json\b[^\n\r|;]*--full\b", re.IGNORECASE
        ),
        "Use compact `--json` for terminal work, or redirect `--json --full` output to a file.",
    ),
    (
        re.compile(r"\bConvertTo-Json\b", re.IGNORECASE),
        "Pipe to `Select-Object -First <n>` before ConvertTo-Json, or redirect the full JSON to a file.",
    ),
)


def hook_response(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def command_from_tool_input(tool_input: object) -> str:
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "cmd", "script"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(tool_input, sort_keys=True)


def policy_from_env(env: Mapping[str, str] | None = None) -> cyntox_privacy.PrivacyPolicy:
    active_env: Mapping[str, str] = os.environ if env is None else env
    mode = active_env.get("CYNTOX_INTERNET_MODE", "off").strip().lower() or "off"
    domains = tuple(
        domain.strip()
        for domain in re.split(r"[,;]", active_env.get("CYNTOX_ALLOW_DOMAINS", ""))
        if domain.strip()
    )
    return cyntox_privacy.PrivacyPolicy(mode, domains)


def unbounded_output_reason(command: str) -> str | None:
    if OUTPUT_BOUND_RE.search(command):
        return None
    if RG_COMMAND_RE.search(command) and not RG_SINGLE_FILE_SCOPE_RE.search(command):
        return (
            "CyntOX terminal flood guard blocked likely unbounded ripgrep output. "
            "Use `rg -n -m 50 <pattern> <specific path>`, search one specific file, "
            "pipe to `Select-Object -First <n>`, or redirect full results to a file."
        )
    for pattern, hint in UNBOUNDED_OUTPUT_RULES:
        if pattern.search(command):
            return f"CyntOX terminal flood guard blocked likely unbounded output. {hint}"
    return None


def evaluate_pre_tool_use(
    payload: dict[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    tool_name = str(payload.get("tool_name") or "")
    if tool_name not in SHELL_TOOL_NAMES:
        return hook_response("allow", "CyntOX shell privacy guard only handles shell tools.")

    command = command_from_tool_input(payload.get("tool_input"))
    if not command.strip():
        return hook_response("deny", "CyntOX blocked an empty or unreadable shell command.")

    flood_reason = unbounded_output_reason(command)
    if flood_reason:
        return hook_response("deny", flood_reason)

    policy = policy_from_env(env)
    scan = cyntox_privacy.scan_text(command)
    network_policy = cyntox_privacy.evaluate_network_policy(
        [str(url) for url in scan["urls"]], policy
    )
    denied_urls = network_policy.get("denied_urls") or []

    if denied_urls:
        domains = sorted(
            {str(item.get("domain")) for item in denied_urls if isinstance(item, dict)}
        )
        return hook_response(
            "deny", f"CyntOX privacy blocks public network access to: {', '.join(domains)}."
        )

    if SECRET_WORD_RE.search(command) and NETWORK_COMMAND_RE.search(command):
        return hook_response(
            "deny",
            "CyntOX privacy blocks shell commands that combine network activity with secret-like data.",
        )

    if NETWORK_COMMAND_RE.search(command):
        if policy.internet_mode == "off":
            return hook_response(
                "deny",
                "CyntOX privacy blocks network-like shell commands while internet mode is off.",
            )
        return hook_response(
            "ask",
            "CyntOX detected a network-like shell command. Confirm the destination and data scope.",
        )

    return hook_response(
        "ask", "CyntOX shell privacy guard requires confirmation before shell execution."
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print("CyntOX blocked malformed hook input.", file=sys.stderr)
        print(json.dumps(hook_response("deny", "CyntOX blocked malformed hook input.")))
        return 0
    if not isinstance(payload, dict):
        payload = {}
    print(json.dumps(evaluate_pre_tool_use(payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
