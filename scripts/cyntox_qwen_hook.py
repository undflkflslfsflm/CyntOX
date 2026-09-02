from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

try:
    from scripts import cyntox_privacy
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    import cyntox_privacy  # type: ignore[no-redef]


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


def policy_from_env(env: dict[str, str] | None = None) -> cyntox_privacy.PrivacyPolicy:
    env = env or os.environ
    mode = env.get("CYNTOX_INTERNET_MODE", "off").strip().lower() or "off"
    domains = tuple(
        domain.strip()
        for domain in re.split(r"[,;]", env.get("CYNTOX_ALLOW_DOMAINS", ""))
        if domain.strip()
    )
    return cyntox_privacy.PrivacyPolicy(mode, domains)


def evaluate_pre_tool_use(payload: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, Any]:
    tool_name = str(payload.get("tool_name") or "")
    if tool_name not in SHELL_TOOL_NAMES:
        return hook_response("allow", "CyntOX shell privacy guard only handles shell tools.")

    command = command_from_tool_input(payload.get("tool_input"))
    if not command.strip():
        return hook_response("deny", "CyntOX blocked an empty or unreadable shell command.")

    policy = policy_from_env(env)
    scan = cyntox_privacy.scan_text(command)
    network_policy = cyntox_privacy.evaluate_network_policy([str(url) for url in scan["urls"]], policy)
    denied_urls = network_policy.get("denied_urls") or []

    if denied_urls:
        domains = sorted({str(item.get("domain")) for item in denied_urls if isinstance(item, dict)})
        return hook_response("deny", f"CyntOX privacy blocks public network access to: {', '.join(domains)}.")

    if SECRET_WORD_RE.search(command) and NETWORK_COMMAND_RE.search(command):
        return hook_response("deny", "CyntOX privacy blocks shell commands that combine network activity with secret-like data.")

    if NETWORK_COMMAND_RE.search(command):
        if policy.internet_mode == "off":
            return hook_response("deny", "CyntOX privacy blocks network-like shell commands while internet mode is off.")
        return hook_response("ask", "CyntOX detected a network-like shell command. Confirm the destination and data scope.")

    return hook_response("ask", "CyntOX shell privacy guard requires confirmation before shell execution.")


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
