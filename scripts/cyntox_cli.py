from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    from scripts import cyntox_council, cyntox_memory, cyntox_privacy
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    import cyntox_council  # type: ignore[import-not-found,no-redef]
    import cyntox_memory  # type: ignore[import-not-found,no-redef]
    import cyntox_privacy  # type: ignore[import-not-found,no-redef]


DEFAULT_JOBS_DIR = ".oslab/cyntox/jobs"
DEFAULT_DEVICES_FILE = "devices.toml"
DEFAULT_QUALITY_TARGET = 9.0
DEFAULT_MAX_RETRIES = 1
MIN_DAILY_MAX_TOKENS = 8192
MIN_DAILY_NUM_CTX = 32768
DEFAULT_PROXY_PORT = 11437
LEGACY_PROXY_PORT = 11436
MAX_STRESS_REPEAT = 25
MAX_STRESS_RERUN_FAILURES = 5
DEFAULT_FOREGROUND_OUTPUT_LIMIT = 4_000
MAX_FOREGROUND_OUTPUT_LIMIT = 50_000
TERMINAL_JSON_STRING_LIMIT = 1_200
TERMINAL_JSON_LIST_LIMIT = 25
TERMINAL_JSON_DICT_LIMIT = 80
TERMINAL_JSON_DEPTH_LIMIT = 6
JOB_LIST_TASK_PREVIEW_LIMIT = 160
JOB_SHOW_TASK_PREVIEW_LIMIT = 800
JOB_SHOW_OUTPUT_PREVIEW_LIMIT = 1_600
JOB_SHOW_ERROR_PREVIEW_LIMIT = 800
DEVICE_SHOW_PREVIEW_LIMIT = 500
WORKER_LOG_TAIL_LIMIT = 4_000
PROCESS_LEAK_SETTLE_SECONDS = 1.5
DEFAULT_STRESS_HISTORY_KEEP = 50
JOB_STATES = {"queued", "running", "needs_input", "failed", "done"}
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
WRITE_WORDS = {
    "add",
    "apply",
    "change",
    "configure",
    "delete",
    "disable",
    "enable",
    "install",
    "move",
    "remove",
    "restart",
    "set",
    "setup",
    "start",
    "stop",
    "uninstall",
    "update",
    "upgrade",
    "write",
}
DENIED_TASK_PATTERNS = (
    re.compile(
        r"\bformat(?:\.com|\.exe)?\b(?:\s+(?:[a-z]:|disk|drive|volume|partition)|\s+/fs:)",
        re.IGNORECASE,
    ),
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/(?:s|q)\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\breboot\b", re.IGNORECASE),
    re.compile(
        r"\b(?:scan|stress|fuzz|attack|ddos|dos)\b.{0,60}\bpublic\s+ips?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bpublic\s+ips?\b.{0,60}\b(?:scan|stress|fuzz|attack|ddos|dos)\b",
        re.IGNORECASE,
    ),
)
SSH_HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]-]+$")
SSH_USER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SERVICE_SKILLS = {
    "jellyfin": ["media-server"],
    "smb": ["media-server", "pc-admin"],
    "nfs": ["media-server", "pc-admin"],
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_project_child(root: Path, child: Path) -> Path:
    child_resolved = child.resolve()
    root_resolved = root.resolve()
    if not (child_resolved == root_resolved or root_resolved in child_resolved.parents):
        raise ValueError(f"Refusing path outside project root: {child_resolved}")
    return child_resolved


def current_python(root: Path) -> str:
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def jobs_root(root: Path, jobs_dir: str = DEFAULT_JOBS_DIR) -> Path:
    return ensure_project_child(root, root / jobs_dir)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json_object(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return loaded


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": utc_now_iso(), **payload}, sort_keys=True) + "\n")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def new_job_id() -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def load_job(
    root: Path, job_id: str, jobs_dir: str = DEFAULT_JOBS_DIR
) -> tuple[Path, dict[str, Any]]:
    base = jobs_root(root, jobs_dir)
    job_dir = ensure_project_child(root, base / job_id)
    job_path = job_dir / "job.json"
    if not job_path.exists():
        raise ValueError(f"No cyntox job found: {job_id}")
    return job_dir, read_json_object(job_path)


def save_job(job_dir: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = utc_now_iso()
    atomic_write_json(job_dir / "job.json", job)


def set_job_state(job_dir: Path, job: dict[str, Any], state: str, **fields: Any) -> None:
    if state not in JOB_STATES:
        raise ValueError(f"Unsupported job state: {state}")
    job["state"] = state
    for key, value in fields.items():
        job[key] = value
    save_job(job_dir, job)
    append_jsonl(job_dir / "events.jsonl", {"event": "state", "state": state, "fields": fields})


def infer_skills(task: str, *, service: str | None = None) -> list[str]:
    text = task.lower()
    tokens = set(re.findall(r"[a-z0-9_-]+", text))
    inferred: list[str] = []
    if service and service.lower() in SERVICE_SKILLS:
        inferred.extend(SERVICE_SKILLS[service.lower()])
    keyword_map = [
        (("jellyfin", "plex", "media", "transcode", "smb", "nfs"), "media-server"),
        (("raspberry", "pi", "disk", "service", "process", "uptime", "log"), "pc-admin"),
        (("os lab", "qemu", "fuzz", "stress", "boot", "kernel"), "os-lab"),
        (("summarize", "research", "notes", "checklist", "sources"), "research-notes"),
        (("privacy", "internet", "prompt injection", "secret", "token"), "privacy-security"),
        (("code", "debug", "fix", "test", "refactor", "repo"), "coding"),
    ]
    for words, skill in keyword_map:
        matched = any(
            word in tokens if len(word) <= 3 and " " not in word else word in text for word in words
        )
        if matched:
            inferred.append(skill)
    return list(dict.fromkeys(inferred))


def create_job(
    root: Path,
    task: str,
    *,
    kind: str = "task",
    preset: str = cyntox_council.DEFAULT_PRESET,
    mode: str = "plan",
    skills: list[str] | None = None,
    device: str | None = None,
    service: str | None = None,
    dry_run: bool = False,
    save_memory: bool = True,
    jobs_dir: str = DEFAULT_JOBS_DIR,
    max_wall_time: str = "3m",
    retry_of: str | None = None,
    requires_approval: bool = False,
    needs_input_reason: str | None = None,
) -> tuple[str, Path, dict[str, Any]]:
    job_id = new_job_id()
    job_dir = jobs_root(root, jobs_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    normalized_skills = [
        cyntox_council.slugify_skill_name(name)
        for name in (skills or infer_skills(task, service=service))
    ]
    normalized_skills = list(dict.fromkeys(normalized_skills))
    state = "needs_input" if needs_input_reason else "queued"
    job = {
        "version": 1,
        "id": job_id,
        "kind": kind,
        "task": task,
        "state": state,
        "mode": mode,
        "preset": preset,
        "quality_target": DEFAULT_QUALITY_TARGET,
        "max_retries": DEFAULT_MAX_RETRIES,
        "max_wall_time": max_wall_time,
        "internet_mode": "off",
        "skills": normalized_skills,
        "device": device,
        "service": service,
        "dry_run": dry_run,
        "save_memory": save_memory,
        "requires_approval": requires_approval,
        "needs_input_reason": needs_input_reason,
        "retry_of": retry_of,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "artifacts": {
            "job": "job.json",
            "prompt": "prompt.md",
            "output": "output.md",
            "commands": "commands.jsonl",
            "events": "events.jsonl",
            "errors": "errors.log",
            "verification": "verification.md",
            "score": "score.json",
            "memory": "memory.md",
        },
    }
    (job_dir / "prompt.md").write_text(task.rstrip() + "\n", encoding="utf-8")
    (job_dir / "output.md").write_text("", encoding="utf-8")
    (job_dir / "commands.jsonl").write_text("", encoding="utf-8")
    (job_dir / "events.jsonl").write_text("", encoding="utf-8")
    (job_dir / "errors.log").write_text("", encoding="utf-8")
    (job_dir / "verification.md").write_text("", encoding="utf-8")
    (job_dir / "score.json").write_text("{}\n", encoding="utf-8")
    if needs_input_reason:
        (job_dir / "output.md").write_text(needs_input_reason.rstrip() + "\n", encoding="utf-8")
    atomic_write_json(job_dir / "job.json", job)
    append_jsonl(job_dir / "events.jsonl", {"event": "created", "state": state})
    return job_id, job_dir, job


def task_has_denied_pattern(task: str) -> str | None:
    for pattern in DENIED_TASK_PATTERNS:
        if pattern.search(task):
            return pattern.pattern
    return None


def job_uses_execution_surface(job: dict[str, Any]) -> bool:
    return (
        str(job.get("mode") or "plan") == "implement"
        or str(job.get("kind") or "task") in {"run-on", "setup"}
        or bool(job.get("device"))
        or bool(job.get("service"))
    )


def task_needs_write(task: str, *, service: str | None = None) -> bool:
    if service:
        return True
    words = set(re.findall(r"[a-z0-9_-]+", task.lower()))
    return bool(words & WRITE_WORDS)


def load_devices(root: Path, devices_file: str = DEFAULT_DEVICES_FILE) -> dict[str, Any]:
    path = ensure_project_child(root, root / devices_file)
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    devices = loaded.get("devices", {}) if isinstance(loaded, dict) else {}
    return devices if isinstance(devices, dict) else {}


def device_status(device: dict[str, Any]) -> tuple[str, list[str]]:
    issues: list[str] = []
    connection = str(device.get("connection") or "").lower()
    configured = bool(device.get("configured"))
    if not configured:
        issues.append("configured=false")
    if connection not in {"local", "ssh", "smb", "filesystem", "api", "rdp"}:
        issues.append("missing-or-unsupported-connection")
    allowed_commands = device.get("allowed_commands")
    denied_commands = device.get("denied_commands")
    if configured and (not isinstance(allowed_commands, list) or not allowed_commands):
        issues.append("allowed_commands-missing")
    if configured and not isinstance(denied_commands, list):
        issues.append("denied_commands-missing")
    if connection == "ssh":
        issues.extend(ssh_target_issues(device))
    if connection in {"ssh", "smb", "api", "rdp"} and not configured:
        return "needs_config", issues
    if issues:
        return "limited", issues
    return "ready", issues


def resolve_device(
    root: Path, name: str, devices_file: str = DEFAULT_DEVICES_FILE
) -> dict[str, Any]:
    devices = load_devices(root, devices_file)
    device = devices.get(name)
    if not isinstance(device, dict):
        known = ", ".join(sorted(devices)) or "none"
        raise ValueError(f"Unknown device: {name}. Known devices: {known}")
    return device


def unsafe_ssh_value_reason(value: str, *, pattern: re.Pattern[str]) -> str | None:
    if not value:
        return "missing"
    if value.startswith("-"):
        return "starts-with-dash"
    if any(char.isspace() for char in value):
        return "contains-whitespace"
    if not pattern.fullmatch(value):
        return "contains-unsupported-characters"
    return None


def ssh_target_issues(device: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    host = str(device.get("host") or "")
    user = str(device.get("user") or "")
    host_reason = unsafe_ssh_value_reason(host, pattern=SSH_HOST_RE)
    if host_reason:
        issues.append(f"ssh-host-{host_reason}")
    if user:
        user_reason = unsafe_ssh_value_reason(user, pattern=SSH_USER_RE)
        if user_reason:
            issues.append(f"ssh-user-{user_reason}")
    return issues


def ssh_target(device: dict[str, Any]) -> str:
    issues = ssh_target_issues(device)
    if issues:
        raise ValueError(f"Unsafe SSH target in devices.toml: {', '.join(issues)}")
    host = str(device.get("host") or "")
    user = str(device.get("user") or "")
    return f"{user}@{host}" if user else host


def render_device_value(value: Any) -> str:
    if isinstance(value, list):
        return cyntox_memory.excerpt(
            ", ".join(str(item) for item in value), DEVICE_SHOW_PREVIEW_LIMIT
        )
    return cyntox_memory.excerpt(str(value), DEVICE_SHOW_PREVIEW_LIMIT)


def render_device_summary(
    name: str, device: dict[str, Any], *, status: str, issues: list[str]
) -> str:
    display_name = str(device.get("name") or name)
    lines = [
        f"CyntOX device {name}",
        f"Name: {display_name}",
        f"Status: {status}",
        f"Type: {device.get('type', '-')}",
        f"Host: {device.get('host', '-')}",
        f"Connection: {device.get('connection', '-')}",
        f"Configured: {bool(device.get('configured'))}",
        f"Approved writes: {bool(device.get('approved_writes'))}",
    ]
    if issues:
        lines.append(f"Issues: {', '.join(issues)}")
    else:
        lines.append("Issues: none")

    for field in ("allowed_services", "allowed_commands", "denied_commands", "paths", "notes"):
        if field in device:
            lines.append(f"{field.replace('_', ' ').title()}: {render_device_value(device[field])}")
    lines.append(
        "Use --json for compact parseable metadata or --json --full for full device metadata."
    )
    return "\n".join(lines)


def planned_device_commands(
    kind: str, task: str, device_name: str, device: dict[str, Any]
) -> list[str]:
    service = kind.lower()
    task_text = task.lower()
    device_type = str(device.get("type") or "").lower()
    connection = str(device.get("connection") or "unknown")

    if service == "jellyfin":
        if device_type == "windows-pc" or connection == "local":
            return [
                "winget search Jellyfin.Server",
                "winget install Jellyfin.Server",
                "Get-Service *jellyfin*",
                "Open http://localhost:8096 for first-run setup after install",
            ]
        if "raspberry" in device_type or "linux" in device_type:
            return [
                "sudo apt-get update",
                "sudo apt-get install -y jellyfin",
                "systemctl status jellyfin --no-pager",
            ]
    if "uptime" in task_text:
        if device_type == "windows-pc" or connection == "local":
            return ["Get-CimInstance Win32_OperatingSystem | Select-Object LastBootUpTime"]
        return ["uptime"]
    if "disk" in task_text:
        if device_type == "windows-pc" or connection == "local":
            return ["Get-PSDrive -PSProvider FileSystem"]
        return ["df -h"]
    if "service" in task_text:
        if device_type == "windows-pc" or connection == "local":
            return ["Get-Service | Select-Object -First 20"]
        return ["systemctl --failed --no-pager"]
    return [f"# No deterministic executor mapping yet for {device_name!r}; council plan only."]


def render_device_context(
    *,
    command_kind: str,
    task: str,
    device_name: str | None,
    device: dict[str, Any] | None,
    planned_commands: list[str],
    dry_run: bool,
    execution_output: str,
) -> str:
    if not device_name or not device:
        return task
    status, issues = device_status(device)
    device_lines = [
        "CyntOX device-scoped job.",
        "",
        "Safety requirements:",
        "- Default to local/offline; no public internet unless the user explicitly allowlists it.",
        "- Do not claim remote control, install success, or service success without command output.",
        "- First write/install requires recorded approval for the target device.",
        "- Dry-run means planned commands only; no remote mutation.",
        "",
        f"Command kind: {command_kind}",
        f"Target device: {device_name}",
        f"Device type: {device.get('type', 'unknown')}",
        f"Connection: {device.get('connection', 'unknown')}",
        f"Host: {device.get('host', 'unknown')}",
        f"Configured status: {status}",
        f"Issues: {', '.join(issues) if issues else 'none'}",
        f"Approved writes: {bool(device.get('approved_writes'))}",
        f"Dry run: {dry_run}",
        "",
        "Planned commands:",
        *[f"- {command}" for command in planned_commands],
    ]
    if execution_output:
        device_lines.extend(["", "Executor output:", execution_output])
    device_lines.extend(["", "User task:", task])
    return "\n".join(device_lines)


def device_denied_command_match(device: dict[str, Any], command: str) -> str | None:
    denied_commands = device.get("denied_commands")
    if not isinstance(denied_commands, list):
        return None
    lowered = command.lower()
    for denied in denied_commands:
        needle = str(denied).strip().lower()
        if not needle:
            continue
        if re.search(r"\s", needle):
            matched = needle in lowered
        else:
            matched = bool(
                re.search(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(needle)}(?![A-Za-z0-9_.-])",
                    lowered,
                )
            )
        if matched:
            return str(denied)
    return None


def command_policy_labels(command: str) -> set[str]:
    lowered = command.lower()
    labels = {"read-only diagnostics"}
    if lowered == "uptime" or "lastbootuptime" in lowered:
        labels.add("uptime")
    if lowered == "df -h" or lowered.startswith("df ") or "get-psdrive" in lowered:
        labels.add("disk inventory")
        labels.add("read-only free-space check")
    if (
        "get-service" in lowered
        or lowered.startswith("systemctl status ")
        or lowered == "systemctl --failed --no-pager"
    ):
        labels.add("service status")
    if lowered == "hostnamectl":
        labels.add("host inventory")
    return labels


def device_allowed_command_match(device: dict[str, Any], command: str) -> str | None:
    allowed_commands = device.get("allowed_commands")
    if not isinstance(allowed_commands, list) or not allowed_commands:
        return None
    lowered = command.strip().lower()
    labels = command_policy_labels(command)
    for allowed in allowed_commands:
        policy = str(allowed).strip()
        if not policy:
            continue
        policy_lower = policy.lower()
        if lowered == policy_lower or policy_lower in labels:
            return policy
        if re.search(r"\s", policy_lower) and policy_lower in lowered:
            return policy
    return None


def device_allowed_service_match(device: dict[str, Any], service: str) -> str | None:
    allowed_services = device.get("allowed_services")
    if not isinstance(allowed_services, list) or not allowed_services:
        return "implicit service allow"
    requested = service.strip().lower()
    for allowed in allowed_services:
        policy = str(allowed).strip()
        if policy.lower() == requested:
            return policy
    return None


def find_powershell() -> str:
    for candidate in ("pwsh.exe", "powershell.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError("PowerShell was not found.")


def safe_text_capture_kwargs(timeout: float | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "capture_output": True,
        "check": False,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


def run_local_readonly_command(root: Path, command: str) -> subprocess.CompletedProcess[str]:
    shell = find_powershell()
    completed = subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-Command", command],
        cwd=root,
        **safe_text_capture_kwargs(timeout=30),
    )
    return completed


def run_ssh_readonly_command(
    root: Path,
    device: dict[str, Any],
    command: str,
) -> subprocess.CompletedProcess[str]:
    ssh = shutil.which("ssh.exe") or shutil.which("ssh")
    if not ssh:
        raise RuntimeError("ssh executable was not found.")
    target = ssh_target(device)
    completed = subprocess.run(  # noqa: S603
        [ssh, target, command],
        cwd=root,
        **safe_text_capture_kwargs(timeout=30),
    )
    return completed


def execute_readonly_device_commands(
    root: Path,
    device: dict[str, Any],
    commands: list[str],
) -> str:
    connection = str(device.get("connection") or "").lower()
    outputs: list[str] = []
    for command in commands:
        if command.startswith("#"):
            continue
        denied = device_denied_command_match(device, command)
        if denied:
            outputs.append(f"SKIPPED denied command policy {denied!r}: {command}")
            continue
        if task_needs_write(command):
            outputs.append(f"SKIPPED write-like command: {command}")
            continue
        allowed = device_allowed_command_match(device, command)
        if allowed is None:
            outputs.append(f"SKIPPED not in allowed command policy: {command}")
            continue
        if connection in {"local", "filesystem"}:
            completed = run_local_readonly_command(root, command)
        elif connection == "ssh":
            completed = run_ssh_readonly_command(root, device, command)
        else:
            outputs.append(f"SKIPPED unsupported execution channel {connection!r}: {command}")
            continue
        outputs.append(
            "\n".join(
                [
                    f"$ {command}",
                    f"returncode={completed.returncode}",
                    completed.stdout.strip(),
                    completed.stderr.strip(),
                ]
            ).strip()
        )
    return "\n\n".join(output for output in outputs if output)


def newest_child_dir(path: Path) -> Path | None:
    if not path.exists():
        return None
    dirs = [child for child in path.iterdir() if child.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda child: child.stat().st_mtime)


def copy_council_artifacts(job_dir: Path, council_out_dir: Path) -> dict[str, Any]:
    run_dir = newest_child_dir(council_out_dir)
    if run_dir is None:
        return {"run_dir": None, "overall": None, "scorecard": None, "passed_threshold": False}
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return {
            "run_dir": str(run_dir),
            "overall": None,
            "scorecard": None,
            "passed_threshold": False,
        }
    manifest = read_json_object(manifest_path)
    final_output = ""
    for result in manifest.get("results", []):
        if not isinstance(result, dict):
            continue
        role = str(result.get("role") or "")
        output_path = result.get("output")
        if "synthesizer" in role and isinstance(output_path, str):
            candidate = Path(output_path)
            if candidate.exists():
                final_output = candidate.read_text(encoding="utf-8", errors="replace").strip()
    if not final_output:
        final_output = (job_dir / "output.md").read_text(encoding="utf-8", errors="replace").strip()
    (job_dir / "output.md").write_text(final_output.rstrip() + "\n", encoding="utf-8")

    scorecard = manifest.get("latest_scorecard")
    overall = manifest.get("latest_score")
    score_payload = {
        "overall": overall,
        "scorecard": scorecard,
        "passed_threshold": bool(manifest.get("passed_threshold")),
        "retries_used": manifest.get("retries_used"),
        "run_dir": str(run_dir),
    }
    (job_dir / "score.json").write_text(json.dumps(score_payload, indent=2), encoding="utf-8")
    return score_payload


def run_job(root: Path, job_id: str, jobs_dir: str = DEFAULT_JOBS_DIR) -> int:
    job_dir, job = load_job(root, job_id, jobs_dir)
    if job.get("state") == "done":
        append_jsonl(job_dir / "events.jsonl", {"event": "worker_skip", "reason": "already_done"})
        return 0

    set_job_state(job_dir, job, "running", pid=os.getpid(), started_at=utc_now_iso())
    council_out_dir = job_dir / "council-runs"
    device: dict[str, Any] | None = None
    planned_commands: list[str] = []
    execution_output = ""
    try:
        denied = task_has_denied_pattern(str(job.get("task") or ""))
        if denied and job_uses_execution_surface(job):
            raise ValueError(f"Task matches denied device/action pattern: {denied}")
        if denied:
            append_jsonl(
                job_dir / "events.jsonl",
                {"event": "deny_pattern_observed_plan_only", "pattern": denied},
            )

        device_name = job.get("device")
        service = job.get("service")
        if isinstance(device_name, str) and device_name:
            device = resolve_device(root, device_name)
            if (
                isinstance(service, str)
                and service
                and device_allowed_service_match(device, service) is None
            ):
                allowed = device.get("allowed_services")
                reason = (
                    f"Service {service!r} is not allowed for target {device_name}. "
                    f"Allowed services: {', '.join(str(item) for item in allowed) if isinstance(allowed, list) and allowed else 'none'}."
                )
                (job_dir / "output.md").write_text(reason + "\n", encoding="utf-8")
                (job_dir / "verification.md").write_text(
                    "No setup planning or execution was run because the target service is not allowed.\n",
                    encoding="utf-8",
                )
                set_job_state(job_dir, job, "needs_input", needs_input_reason=reason)
                return 2
            planned_commands = planned_device_commands(
                str(service or ""),
                str(job.get("task") or ""),
                device_name,
                device,
            )
            for command in planned_commands:
                append_jsonl(
                    job_dir / "commands.jsonl",
                    {"kind": "planned", "device": device_name, "command": command},
                )
            status, issues = device_status(device)
            requires_write = task_needs_write(
                str(job.get("task") or ""), service=str(service) if service else None
            )
            if (
                requires_write
                and not bool(job.get("dry_run"))
                and not bool(device.get("approved_writes"))
            ):
                reason = (
                    f"Approval required before first write/install on {device_name}. "
                    f"Device status={status}; issues={', '.join(issues) if issues else 'none'}. "
                    "Run with --dry-run first, then record approval in devices.toml."
                )
                (job_dir / "output.md").write_text(reason + "\n", encoding="utf-8")
                (job_dir / "verification.md").write_text(
                    "No remote write/install was executed. Approval is required.\n",
                    encoding="utf-8",
                )
                set_job_state(job_dir, job, "needs_input", needs_input_reason=reason)
                return 2
            if not bool(job.get("dry_run")) and status == "ready" and not requires_write:
                execution_output = execute_readonly_device_commands(root, device, planned_commands)
                append_jsonl(
                    job_dir / "commands.jsonl",
                    {"kind": "executor-output", "device": device_name, "output": execution_output},
                )

        task = render_device_context(
            command_kind=str(job.get("kind") or "task"),
            task=str(job.get("task") or ""),
            device_name=str(job.get("device") or "") or None,
            device=device,
            planned_commands=planned_commands,
            dry_run=bool(job.get("dry_run")),
            execution_output=execution_output,
        )
        council_args = [
            "--preset",
            str(job.get("preset") or cyntox_council.DEFAULT_PRESET),
            "--mode",
            str(job.get("mode") or "plan"),
            "--pass-threshold",
            str(job.get("quality_target") or DEFAULT_QUALITY_TARGET),
            "--max-retries",
            str(job.get("max_retries") or DEFAULT_MAX_RETRIES),
            "--max-wall-time",
            str(job.get("max_wall_time") or "3m"),
            "--out-dir",
            council_out_dir.relative_to(root).as_posix(),
            "--internet-mode",
            str(job.get("internet_mode") or "off"),
        ]
        if bool(job.get("dry_run")):
            council_args.append("--dry-run")
        if bool(job.get("save_memory")) and not bool(job.get("dry_run")):
            council_args.append("--save-memory")
        for skill_name in job.get("skills", []):
            council_args.extend(["--use-skill", str(skill_name)])
        council_args.append(task)
        append_jsonl(job_dir / "commands.jsonl", {"kind": "council", "argv": council_args})

        completed = subprocess.run(  # noqa: S603
            [current_python(root), str(root / "scripts" / "cyntox_council.py"), *council_args],
            cwd=root,
            **safe_text_capture_kwargs(),
        )
        if completed.stderr.strip():
            append_text(job_dir / "errors.log", completed.stderr)
        if completed.stdout.strip():
            (job_dir / "output.md").write_text(completed.stdout.strip() + "\n", encoding="utf-8")
        score_payload = copy_council_artifacts(job_dir, council_out_dir)
        job["latest_score"] = score_payload.get("overall")
        job["score"] = score_payload
        job["council_run_dir"] = score_payload.get("run_dir")
        verification = [
            f"# Verification for CyntOX job {job_id}",
            "",
            f"Council return code: {completed.returncode}",
            f"Dry run: {bool(job.get('dry_run'))}",
            f"Council run dir: {score_payload.get('run_dir')}",
            f"Overall score: {score_payload.get('overall')}",
            f"Passed threshold: {score_payload.get('passed_threshold')}",
        ]
        if device:
            verification.extend(
                [
                    "",
                    "## Device evidence",
                    "",
                    f"Device: {job.get('device')}",
                    f"Dry-run only: {bool(job.get('dry_run'))}",
                    execution_output or "No device executor command was run.",
                ]
            )
        (job_dir / "verification.md").write_text(
            "\n".join(verification).rstrip() + "\n", encoding="utf-8"
        )

        overall = score_payload.get("overall")
        passed = bool(score_payload.get("passed_threshold"))
        if completed.returncode != 0:
            set_job_state(job_dir, job, "failed", returncode=completed.returncode)
            return completed.returncode
        if not bool(job.get("dry_run")) and not isinstance(overall, int | float):
            set_job_state(job_dir, job, "failed", returncode=1, failure="missing_score")
            return 1
        if not bool(job.get("dry_run")) and not passed:
            set_job_state(job_dir, job, "failed", returncode=1, failure="below_quality_target")
            return 1
        if bool(job.get("save_memory")) and not bool(job.get("dry_run")):
            try:
                note = cyntox_memory.extract_job_memory(root, job_id, jobs_dir=jobs_dir)
                append_jsonl(
                    job_dir / "events.jsonl", {"event": "memory_extracted", "note": str(note)}
                )
            except ValueError as error:
                append_jsonl(
                    job_dir / "events.jsonl", {"event": "memory_skipped", "reason": str(error)}
                )
        set_job_state(job_dir, job, "done", returncode=0, completed_at=utc_now_iso())
        return 0
    except Exception as error:  # noqa: BLE001
        append_text(job_dir / "errors.log", f"{type(error).__name__}: {error}")
        (job_dir / "output.md").write_text(f"CyntOX job failed: {error}\n", encoding="utf-8")
        set_job_state(job_dir, job, "failed", returncode=1, failure=str(error))
        return 1


def start_worker(root: Path, job_id: str, jobs_dir: str = DEFAULT_JOBS_DIR) -> int:
    job_dir, job = load_job(root, job_id, jobs_dir)
    stdout_path = job_dir / "worker.stdout.log"
    stderr_path = job_dir / "worker.stderr.log"
    command = [
        current_python(root),
        str(root / "scripts" / "cyntox_cli.py"),
        "_worker",
        job_id,
        "--jobs-dir",
        jobs_dir,
    ]
    creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    job["worker_pid"] = process.pid
    save_job(job_dir, job)
    append_jsonl(job_dir / "events.jsonl", {"event": "worker_started", "pid": process.pid})
    return process.pid


def print_foreground_result(
    root: Path, job_id: str, returncode: int, jobs_dir: str = DEFAULT_JOBS_DIR
) -> None:
    output_path = jobs_root(root, jobs_dir) / job_id / "output.md"
    if output_path.is_file():
        output = output_path.read_text(encoding="utf-8", errors="replace").strip()
        if output:
            limit = cyntox_council.bounded_env_int(
                "CYNTOX_FOREGROUND_OUTPUT_LIMIT",
                default=DEFAULT_FOREGROUND_OUTPUT_LIMIT,
                minimum=0,
                maximum=MAX_FOREGROUND_OUTPUT_LIMIT,
            )
            print(cyntox_council.terminal_preview(output, limit=limit, artifact=output_path))
    print(f"Job {job_id} finished with code {returncode}.")


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    tasklist = shutil.which("tasklist.exe") or shutil.which("tasklist")
    if not tasklist:
        return False
    completed = subprocess.run(  # noqa: S603
        [tasklist, "/FI", f"PID eq {pid}", "/FO", "CSV"],
        **safe_text_capture_kwargs(),
    )
    return str(pid) in completed.stdout


def nested_value(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def add_doctor_check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    message: str,
    **details: Any,
) -> None:
    check: dict[str, Any] = {"name": name, "status": status, "message": message}
    if details:
        check["details"] = details
    checks.append(check)


def combine_doctor_status(checks: list[dict[str, Any]]) -> str:
    statuses = {str(check.get("status")) for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def read_optional_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, str(error)
    if not isinstance(loaded, dict):
        return None, "not a JSON object"
    return loaded, None


def bounded_proxy_port() -> int:
    raw = os.environ.get("OSLAB_CYNTOX_PROXY_PORT", "").strip()
    if raw.isdigit():
        return max(1, min(int(raw), 65535))
    return DEFAULT_PROXY_PORT


def read_proxy_health(port: int) -> tuple[dict[str, Any] | None, str | None]:
    url = f"http://127.0.0.1:{port}/__cyntox_proxy_health"
    request = urllib.request.Request(url, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=1) as response:  # noqa: S310
            loaded = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        return None, str(error)
    if not isinstance(loaded, dict):
        return None, "proxy health returned non-object JSON"
    return loaded, None


def inspect_qwen_package(root: Path, checks: list[dict[str, Any]]) -> None:
    package_path = root / "node_modules" / "@qwen-code" / "qwen-code" / "package.json"
    loaded, error = read_optional_json(package_path)
    if error:
        add_doctor_check(
            checks,
            "qwen package",
            "fail",
            "Qwen Code package is missing; run npm install/bootstrap before chat.",
            path=str(package_path),
            error=error,
        )
        return
    version = str((loaded or {}).get("version") or "unknown")
    add_doctor_check(
        checks,
        "qwen package",
        "ok",
        f"Qwen Code package found (v{version}).",
        path=str(package_path),
        version=version,
    )


def inspect_launcher_files(root: Path, checks: list[dict[str, Any]]) -> None:
    required = ["cyntox.cmd", "cyntox.ps1", "qwen-code.cmd", "qwen-code.ps1"]
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        add_doctor_check(
            checks,
            "launcher files",
            "fail",
            "One-line startup is incomplete because launcher files are missing.",
            missing=missing,
        )
    else:
        add_doctor_check(
            checks,
            "launcher files",
            "ok",
            "CyntOX and Qwen compatibility launchers are present.",
        )
    banner = root / ".qwen" / "cyntox-banner.txt"
    add_doctor_check(
        checks,
        "cyntox banner",
        "ok" if banner.is_file() else "warn",
        "Custom CyntOX banner is present."
        if banner.is_file()
        else "Custom CyntOX banner is missing; chat still works but branding falls back.",
        path=str(banner),
    )


def inspect_generated_qwen_settings(root: Path, checks: list[dict[str, Any]]) -> None:
    settings_path = root / ".oslab" / "qwen-code-workspace" / ".qwen" / "settings.json"
    settings, error = read_optional_json(settings_path)
    if error:
        add_doctor_check(
            checks,
            "qwen settings",
            "warn",
            "Generated Qwen settings are not readable yet; run cyntox chat once to regenerate them.",
            path=str(settings_path),
            error=error,
        )
        return
    assert settings is not None
    model_name = str(nested_value(settings, ("model", "name")) or "")
    model_base_url = str(nested_value(settings, ("model", "baseUrl")) or "")
    model_max_tokens = nested_value(
        settings, ("model", "generationConfig", "samplingParams", "max_tokens")
    )
    model_num_ctx = nested_value(
        settings, ("model", "generationConfig", "extra_body", "options", "num_ctx")
    )
    model_context_window = nested_value(
        settings, ("model", "generationConfig", "contextWindowSize")
    )
    ui_mouse_tracking = nested_value(settings, ("ui", "mouseTracking"))
    ui_terminal_buffer = nested_value(settings, ("ui", "useTerminalBuffer"))
    denied_tools = nested_value(settings, ("permissions", "deny"))
    hook_config = nested_value(settings, ("hooks", "PreToolUse"))

    problems: list[str] = []
    if model_name.lower() != "cyntox":
        problems.append(f"model.name={model_name!r}")
    if f":{bounded_proxy_port()}/" not in model_base_url:
        problems.append(f"model.baseUrl={model_base_url!r}")
    if not isinstance(model_max_tokens, int) or model_max_tokens < MIN_DAILY_MAX_TOKENS:
        problems.append(f"max_tokens={model_max_tokens!r}")
    if not isinstance(model_num_ctx, int) or model_num_ctx < MIN_DAILY_NUM_CTX:
        problems.append(f"num_ctx={model_num_ctx!r}")
    if not isinstance(model_context_window, int) or model_context_window < MIN_DAILY_NUM_CTX:
        problems.append(f"contextWindowSize={model_context_window!r}")
    if ui_mouse_tracking is not False:
        problems.append(f"ui.mouseTracking={ui_mouse_tracking!r}")
    if ui_terminal_buffer is not False:
        problems.append(f"ui.useTerminalBuffer={ui_terminal_buffer!r}")
    denied_tool_names = (
        {str(item) for item in denied_tools} if isinstance(denied_tools, list) else set()
    )
    missing_denied_tools = sorted({"display_image", "web_fetch", "web_search"} - denied_tool_names)
    if missing_denied_tools:
        problems.append(f"permissions.deny missing {missing_denied_tools!r}")
    hook_blob = json.dumps(hook_config, sort_keys=True) if hook_config is not None else ""
    if "cyntox_qwen_hook.py" not in hook_blob:
        problems.append("hooks.PreToolUse missing cyntox_qwen_hook.py")

    add_doctor_check(
        checks,
        "qwen settings",
        "fail" if problems else "ok",
        "Generated Qwen settings protect against short output caps, mouse-tracking junk, and terminal floods."
        if not problems
        else "Generated Qwen settings need regeneration; run cyntox chat once.",
        path=str(settings_path),
        problems=problems,
        max_tokens=model_max_tokens,
        num_ctx=model_num_ctx,
        context_window=model_context_window,
        mouse_tracking=ui_mouse_tracking,
        terminal_buffer=ui_terminal_buffer,
        denied_tools=sorted(denied_tool_names),
        terminal_flood_hook="cyntox_qwen_hook.py" in hook_blob,
    )


def inspect_proxy_health(checks: list[dict[str, Any]]) -> None:
    port = bounded_proxy_port()
    health, error = read_proxy_health(port)
    if error:
        add_doctor_check(
            checks,
            "cyntox proxy",
            "warn",
            "CyntOX proxy is not running right now; cyntox chat will start it when needed.",
            port=port,
            error=error,
        )
        return
    assert health is not None
    max_tokens = health.get("max_tokens")
    num_ctx = health.get("num_ctx")
    service = str(health.get("service") or "")
    problems: list[str] = []
    if service != "cyntox-openai-proxy":
        problems.append(f"service={service!r}")
    if not isinstance(max_tokens, int) or max_tokens < MIN_DAILY_MAX_TOKENS:
        problems.append(f"max_tokens={max_tokens!r}")
    if not isinstance(num_ctx, int) or num_ctx < MIN_DAILY_NUM_CTX:
        problems.append(f"num_ctx={num_ctx!r}")
    add_doctor_check(
        checks,
        "cyntox proxy",
        "fail" if problems else "ok",
        "Running proxy has the larger daily-use output/context limits."
        if not problems
        else "Running proxy has stale or too-small generation limits; restart cyntox chat.",
        port=port,
        problems=problems,
        health=health,
    )


def docker_desktop_recent_error() -> dict[str, str] | None:
    if os.name != "nt":
        return None
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    log_path = Path(local_app_data) / "Docker" / "log" / "host" / "com.docker.backend.exe.log"
    if not log_path.is_file():
        return None
    try:
        text = tail_text(log_path.read_text(encoding="utf-8", errors="replace"), 24_000)
    except OSError:
        return None
    lowered = text.lower()
    if "sailor-ingest.sock" in lowered and "file cannot be accessed by the system" in lowered:
        return {
            "diagnostic": (
                "Docker Desktop backend is crashing on stale socket "
                "sailor-ingest.sock: The file cannot be accessed by the system."
            ),
            "remediation": (
                "Quit Docker Desktop, clear the stale socket/reparse artifact under "
                "%LOCALAPPDATA%\\Docker\\run\\sailor-ingest.sock, then start Docker Desktop "
                "again before running `cyntox stress --require-qemu --fix`."
            ),
            "log": str(log_path),
        }
    for marker in (
        "backend crashed, dumping error",
        "docker desktop encountered an unexpected error",
    ):
        if marker in lowered:
            line = next(
                (line.strip() for line in reversed(text.splitlines()) if marker in line.lower()),
                "",
            )
            return {
                "diagnostic": concise_line(line or "Docker Desktop backend crash detected."),
                "remediation": (
                    "Open Docker Desktop's troubleshooting view or inspect the backend log, then "
                    "restart Docker before running `cyntox stress --require-qemu --fix`."
                ),
                "log": str(log_path),
            }
    return None


def inspect_docker_qemu(checks: list[dict[str, Any]]) -> None:
    docker = shutil.which("docker.exe") or shutil.which("docker")
    if not docker:
        add_doctor_check(
            checks,
            "docker/qemu stress",
            "warn",
            "Docker CLI is missing; real QEMU stress tests will be skipped.",
        )
        return
    try:
        completed = subprocess.run(  # noqa: S603
            [docker, "version", "--format", "{{.Server.Version}}"],
            **safe_text_capture_kwargs(timeout=8),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        add_doctor_check(
            checks,
            "docker/qemu stress",
            "warn",
            "Docker did not respond; real QEMU stress tests will be skipped.",
            error=str(error),
        )
        return
    server_version = completed.stdout.strip()
    if completed.returncode == 0 and server_version:
        add_doctor_check(
            checks,
            "docker/qemu stress",
            "ok",
            f"Docker daemon is reachable for optional QEMU stress tests ({server_version}).",
            server_version=server_version,
        )
        return
    docker_error = docker_desktop_recent_error()
    details = docker_error or {}
    add_doctor_check(
        checks,
        "docker/qemu stress",
        "warn",
        "Docker CLI exists, but the daemon/Linux engine is not reachable; QEMU stress tests will skip.",
        stderr=completed.stderr.strip(),
        **details,
    )


def inspect_git_remote(root: Path, checks: list[dict[str, Any]]) -> None:
    git = shutil.which("git.exe") or shutil.which("git")
    if not git:
        add_doctor_check(checks, "git remote", "warn", "Git executable was not found.")
        return
    try:
        completed = subprocess.run(  # noqa: S603
            [git, "remote", "-v"],
            cwd=root,
            **safe_text_capture_kwargs(timeout=5),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        add_doctor_check(
            checks,
            "git remote",
            "warn",
            "Could not inspect git remotes.",
            error=str(error),
        )
        return
    remotes = [line for line in completed.stdout.splitlines() if line.strip()]
    add_doctor_check(
        checks,
        "git remote",
        "ok" if remotes else "warn",
        "Git remote is configured."
        if remotes
        else "No git remote is configured, so private GitHub push cannot run yet.",
        remotes=remotes,
    )


def build_doctor_report(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    inspect_launcher_files(root, checks)
    inspect_qwen_package(root, checks)
    inspect_generated_qwen_settings(root, checks)
    inspect_proxy_health(checks)
    inspect_docker_qemu(checks)
    inspect_git_remote(root, checks)
    status = combine_doctor_status(checks)
    return {
        "status": status,
        "root": str(root),
        "minimums": {
            "max_tokens": MIN_DAILY_MAX_TOKENS,
            "num_ctx": MIN_DAILY_NUM_CTX,
            "proxy_port": bounded_proxy_port(),
        },
        "checks": checks,
    }


def render_doctor_report(report: dict[str, Any]) -> str:
    lines = [
        f"CyntOX doctor: {str(report.get('status', 'unknown')).upper()}",
        f"Root: {report.get('root')}",
        "",
    ]
    for check in report.get("checks", []):
        if not isinstance(check, dict):
            continue
        marker = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}.get(str(check.get("status")), "INFO")
        lines.append(f"[{marker}] {check.get('name')}: {check.get('message')}")
        details = check.get("details")
        if isinstance(details, dict):
            problems = details.get("problems")
            if isinstance(problems, list) and problems:
                lines.append(f"      Problems: {', '.join(str(problem) for problem in problems)}")
            error = details.get("error")
            if error:
                lines.append(f"      Detail: {error}")
            diagnostic = details.get("diagnostic")
            if diagnostic:
                lines.append(f"      Diagnostic: {diagnostic}")
            remediation = details.get("remediation")
            if remediation:
                lines.append(f"      Fix: {remediation}")
    if report.get("status") != "ok":
        lines.extend(
            [
                "",
                "Fast fixes:",
                "  - Run .\\cyntox.cmd chat once to regenerate Qwen settings and restart the proxy.",
                "  - Start Docker Desktop Linux engine before real OS/QEMU stress tests.",
                "  - Add a private Git remote before asking CyntOX to push.",
            ]
        )
    return "\n".join(lines)


def tail_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def concise_line(text: str, limit: int = 240) -> str:
    stripped = " ".join(text.strip().split())
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(0, limit - 3)].rstrip() + "..."


def middle_truncated_text(text: str, limit: int) -> str:
    if limit <= 0:
        return f"[truncated {len(text)} chars]"
    if len(text) <= limit:
        return text
    marker = f"\n[... terminal JSON truncated {len(text) - limit} chars ...]\n"
    if limit <= len(marker) + 20:
        return text[:limit].rstrip() + marker.strip()
    head = max(1, (limit - len(marker)) // 2)
    tail = max(1, limit - len(marker) - head)
    return f"{text[:head]}{marker}{text[-tail:]}"


def compact_terminal_json_value(
    value: Any,
    *,
    changed: list[bool],
    depth: int = 0,
    string_limit: int = TERMINAL_JSON_STRING_LIMIT,
    list_limit: int = TERMINAL_JSON_LIST_LIMIT,
    dict_limit: int = TERMINAL_JSON_DICT_LIMIT,
    depth_limit: int = TERMINAL_JSON_DEPTH_LIMIT,
) -> Any:
    if depth >= depth_limit and isinstance(value, dict | list | tuple):
        changed[0] = True
        return {"_truncated": f"depth limit {depth_limit} reached"}
    if isinstance(value, str):
        if len(value) > string_limit:
            changed[0] = True
            return middle_truncated_text(value, string_limit)
        return value
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        items = list(value.items())
        visible_items = items[:dict_limit]
        if len(items) > len(visible_items):
            changed[0] = True
        compacted: dict[str, Any] = {
            str(key): compact_terminal_json_value(
                item_value,
                changed=changed,
                depth=depth + 1,
                string_limit=string_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth_limit=depth_limit,
            )
            for key, item_value in visible_items
        }
        if len(items) > len(visible_items):
            compacted["_truncated_keys"] = len(items) - len(visible_items)
        return compacted
    if isinstance(value, list | tuple):
        items = list(value)
        visible_items = items[:list_limit]
        if len(items) > len(visible_items):
            changed[0] = True
        compacted_items = [
            compact_terminal_json_value(
                item,
                changed=changed,
                depth=depth + 1,
                string_limit=string_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth_limit=depth_limit,
            )
            for item in visible_items
        ]
        if len(items) > len(visible_items):
            compacted_items.append({"_truncated_items": len(items) - len(visible_items)})
        return compacted_items
    return str(value)


def terminal_json(
    payload: Any, *, full: bool = False, full_artifact: Path | str | None = None
) -> str:
    if full:
        return json.dumps(payload, indent=2)
    changed = [False]
    compacted = compact_terminal_json_value(payload, changed=changed)
    metadata: dict[str, Any] = {
        "compacted": changed[0],
        "string_limit": TERMINAL_JSON_STRING_LIMIT,
        "list_limit": TERMINAL_JSON_LIST_LIMIT,
        "dict_limit": TERMINAL_JSON_DICT_LIMIT,
        "depth_limit": TERMINAL_JSON_DEPTH_LIMIT,
    }
    if full_artifact is not None:
        metadata["full_artifact"] = str(full_artifact)
    if isinstance(compacted, dict):
        compacted = {**compacted, "_cyntox_terminal": metadata}
    else:
        compacted = {"data": compacted, "_cyntox_terminal": metadata}
    return json.dumps(compacted, indent=2)


def diagnostic_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    preferred_patterns = (
        "requires reachable docker daemon",
        "require reachable docker daemon",
        "failed to connect to the docker api",
        "dockerdesktoplinuxengine",
        "would reformat",
        "error:",
        "failed",
    )
    for pattern in preferred_patterns:
        for line in lines:
            if pattern in line.lower():
                return concise_line(line)
    return concise_line(lines[-1])


def stress_result_diagnostic(result: dict[str, Any]) -> str:
    error = str(result.get("error") or "").strip()
    if error:
        return concise_line(error)
    if str(result.get("status")) not in {"warn", "fail"}:
        return ""
    stderr_line = diagnostic_line(str(result.get("stderr_tail") or ""))
    if stderr_line:
        return stderr_line
    return diagnostic_line(str(result.get("stdout_tail") or ""))


def process_leak_patterns(root: Path) -> tuple[str, ...]:
    pytest_root = root / ".oslab" / "pytest"
    raw = str(pytest_root.resolve()).lower()
    return (raw, raw.replace("\\", "/"), raw.replace("/", "\\"))


def powershell_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def process_leaks_from_snapshot(
    root: Path, processes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    patterns = process_leak_patterns(root)
    own_pid = os.getpid()
    leaks: list[dict[str, Any]] = []
    for process in processes:
        try:
            pid = int(process.get("ProcessId") or process.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid == own_pid:
            continue
        command_line = str(process.get("CommandLine") or process.get("command") or "")
        if "Get-CimInstance Win32_Process" in command_line or "process-leak-scan" in command_line:
            continue
        normalized_command = command_line.lower().replace("\\", "/")
        if not any(pattern.replace("\\", "/") in normalized_command for pattern in patterns):
            continue
        leaks.append(
            {
                "pid": pid,
                "parent_pid": process.get("ParentProcessId") or process.get("ppid"),
                "command": concise_line(command_line, limit=500),
            }
        )
    return leaks


def windows_process_snapshot(root: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        shell = find_powershell()
    except RuntimeError as error:
        return [], str(error)
    needle = str((root / ".oslab" / "pytest").resolve()).replace("\\", "/")
    needle_literal = powershell_single_quoted(needle.lower())
    script = (
        f"$needle = {needle_literal}; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and "
        "$_.CommandLine.ToLowerInvariant().Replace('\\','/').Contains($needle) } | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    try:
        completed = subprocess.run(  # noqa: S603
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=root,
            **safe_text_capture_kwargs(timeout=8),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], str(error)
    if completed.returncode != 0:
        return [], completed.stderr.strip() or f"process scan exited {completed.returncode}"
    if not completed.stdout.strip():
        return [], None
    try:
        loaded = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return [], str(error)
    if isinstance(loaded, dict):
        return [loaded], None
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)], None
    return [], "unexpected process snapshot format"


def posix_process_snapshot(root: Path) -> tuple[list[dict[str, Any]], str | None]:
    ps = shutil.which("ps")
    if not ps:
        return [], "ps executable was not found"
    try:
        completed = subprocess.run(  # noqa: S603
            [ps, "-eo", "pid=,ppid=,command="],
            cwd=root,
            **safe_text_capture_kwargs(timeout=8),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], str(error)
    if completed.returncode != 0:
        return [], completed.stderr.strip() or f"process scan exited {completed.returncode}"
    processes: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        processes.append({"pid": parts[0], "ppid": parts[1], "command": parts[2]})
    return processes, None


def scan_process_leaks(root: Path) -> tuple[list[dict[str, Any]], str | None]:
    if os.name == "nt":
        processes, error = windows_process_snapshot(root)
    else:
        processes, error = posix_process_snapshot(root)
    if error:
        return [], error
    return process_leaks_from_snapshot(root, processes), None


def confirmed_process_leaks(
    root: Path, *, settle_seconds: float = PROCESS_LEAK_SETTLE_SECONDS
) -> tuple[list[dict[str, Any]], str | None, int]:
    leaks, error = scan_process_leaks(root)
    if error or not leaks:
        return leaks, error, len(leaks)
    time.sleep(settle_seconds)
    confirmed, second_error = scan_process_leaks(root)
    if second_error:
        return leaks, second_error, len(leaks)
    return confirmed, None, len(leaks)


def process_leak_scan_result(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    leaks, error, first_pass_count = confirmed_process_leaks(root)
    elapsed = round(time.monotonic() - started, 3)
    if error:
        return {
            "name": "process leak scan",
            "command": ["internal", "process-leak-scan"],
            "status": "warn",
            "returncode": 0,
            "elapsed_seconds": elapsed,
            "stdout_tail": "",
            "stderr_tail": error,
        }
    cleared_transient = first_pass_count > 0 and not leaks
    return {
        "name": "process leak scan",
        "command": ["internal", "process-leak-scan"],
        "status": "fail" if leaks else "ok",
        "returncode": 0,
        "elapsed_seconds": elapsed,
        "stdout_tail": f"{len(leaks)} suspected leftover .oslab/pytest process(es)"
        if leaks
        else "transient leak candidates cleared on confirmation"
        if cleared_transient
        else "no suspected .oslab/pytest process leaks",
        "stderr_tail": "",
        "leaks": leaks,
        "first_pass_leak_count": first_pass_count,
    }


def stress_command_status(name: str, returncode: int, stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}".lower()
    if returncode != 0:
        return "fail"
    if name == "qemu stress tests" and (
        "require reachable docker daemon" in combined
        or "requires reachable docker daemon" in combined
        or "failed to connect to the docker api" in combined
        or "dockerdesktoplinuxengine" in combined
    ):
        return "warn"
    return "ok"


def run_stress_command(
    root: Path, name: str, command: list[str], timeout_seconds: int
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=root,
            **safe_text_capture_kwargs(timeout=timeout_seconds),
        )
    except subprocess.TimeoutExpired as error:
        elapsed = round(time.monotonic() - started, 3)
        return {
            "name": name,
            "command": command,
            "status": "fail",
            "returncode": None,
            "elapsed_seconds": elapsed,
            "stdout_tail": tail_text(str(error.stdout or "")),
            "stderr_tail": tail_text(str(error.stderr or "")),
            "error": f"timed out after {timeout_seconds}s",
        }
    except OSError as error:
        elapsed = round(time.monotonic() - started, 3)
        return {
            "name": name,
            "command": command,
            "status": "fail",
            "returncode": None,
            "elapsed_seconds": elapsed,
            "stdout_tail": "",
            "stderr_tail": "",
            "error": str(error),
        }
    elapsed = round(time.monotonic() - started, 3)
    status = stress_command_status(name, completed.returncode, completed.stdout, completed.stderr)
    return {
        "name": name,
        "command": command,
        "status": status,
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "stdout_tail": tail_text(completed.stdout),
        "stderr_tail": tail_text(completed.stderr),
    }


def enforce_required_qemu(result: dict[str, Any], *, require_qemu: bool) -> None:
    if (
        require_qemu
        and result.get("name") == "qemu stress tests"
        and result.get("status") == "warn"
    ):
        result["status"] = "fail"
        result["error"] = (
            "QEMU stress was required, but the QEMU test gate was skipped or only warned. "
            "Start Docker Desktop's Linux engine and retry."
        )


def rerun_failed_stress_command(
    root: Path,
    result: dict[str, Any],
    timeout_seconds: int,
    *,
    rerun_failures: int,
    require_qemu: bool,
) -> None:
    if rerun_failures <= 0 or result.get("status") != "fail":
        return
    name = str(result.get("name") or "")
    command = result.get("command")
    if (
        not name
        or not isinstance(command, list)
        or not all(isinstance(item, str) for item in command)
    ):
        return
    reruns: list[dict[str, Any]] = []
    for attempt in range(1, rerun_failures + 1):
        rerun = run_stress_command(root, name, command, timeout_seconds)
        rerun["rerun_attempt"] = attempt
        rerun["iteration"] = result.get("iteration")
        enforce_required_qemu(rerun, require_qemu=require_qemu)
        reruns.append(rerun)
        if rerun.get("status") == "ok":
            result["status"] = "warn"
            result["recovered_on_rerun"] = True
            result["error"] = (
                "Initial failure recovered on rerun; treat this as flaky until repeated "
                "stress is clean."
            )
            break
    result["reruns"] = reruns


def stress_command_plan(
    root: Path, *, quick: bool, include_qemu: bool, fix: bool
) -> list[tuple[str, list[str], int]]:
    python = current_python(root)
    commands: list[tuple[str, list[str], int]] = []
    if fix:
        commands.append(("format apply", [python, "-m", "ruff", "format", "."], 120))
    commands.extend(
        [
            ("format check", [python, "-m", "ruff", "format", "--check", "."], 120),
            ("lint", [python, "-m", "ruff", "check", "."], 120),
        ]
    )
    if quick:
        commands.append(
            (
                "focused cli tests",
                [python, "-m", "pytest", "tests/unit/test_cyntox_cli.py", "-q"],
                120,
            )
        )
    else:
        commands.extend(
            [
                ("types", [python, "-m", "mypy", "oslab", "scripts"], 180),
                ("full tests", [python, "-m", "pytest", "-q"], 240),
            ]
        )
    if include_qemu:
        commands.append(
            (
                "qemu stress tests",
                [python, "-m", "pytest", "-q", "--run-qemu", "-m", "qemu"],
                180,
            )
        )
    return commands


def write_stress_artifacts(root: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = ensure_project_child(root, root / ".oslab" / "cyntox" / "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    history_dir = reports_dir / "stress"
    history_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(report.get("id") or new_job_id())
    history_json_path = history_dir / f"{run_id}.json"
    history_markdown_path = history_dir / f"{run_id}.md"
    json_path = reports_dir / "stress-latest.json"
    markdown_path = reports_dir / "stress-latest.md"
    report["artifacts"] = {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "history_json": str(history_json_path),
        "history_markdown": str(history_markdown_path),
    }
    atomic_write_json(json_path, report)
    atomic_write_json(history_json_path, report)
    rendered = render_stress_report(report) + "\n"
    markdown_path.write_text(rendered, encoding="utf-8")
    history_markdown_path.write_text(rendered, encoding="utf-8")
    return json_path, markdown_path


def stress_history_dir(root: Path) -> Path:
    return ensure_project_child(root, root / ".oslab" / "cyntox" / "reports" / "stress")


def stress_history_run_ids(root: Path) -> list[str]:
    history_dir = stress_history_dir(root)
    if not history_dir.is_dir():
        return []
    run_ids = {path.stem for path in history_dir.glob("*.json")}
    return sorted(run_ids)


def prune_stress_history(root: Path, *, keep: int) -> list[str]:
    keep = max(1, keep)
    history_dir = stress_history_dir(root)
    if not history_dir.is_dir():
        return []
    run_ids = stress_history_run_ids(root)
    stale_ids = run_ids[: max(0, len(run_ids) - keep)]
    removed: list[str] = []
    for run_id in stale_ids:
        for suffix in (".json", ".md"):
            path = ensure_project_child(root, history_dir / f"{run_id}{suffix}")
            if path.exists():
                path.unlink()
                removed.append(str(path))
    return removed


def stress_history_row(report: dict[str, Any], *, path: Path) -> dict[str, Any]:
    commands = [item for item in report.get("commands", []) if isinstance(item, dict)]
    failed = [str(command.get("name")) for command in commands if command.get("status") == "fail"]
    warned = [str(command.get("name")) for command in commands if command.get("status") == "warn"]
    doctor_checks = nested_value(report, ("doctor", "checks"))
    doctor_warnings = (
        [
            str(check.get("name"))
            for check in doctor_checks
            if isinstance(check, dict) and check.get("status") != "ok"
        ]
        if isinstance(doctor_checks, list)
        else []
    )
    artifacts = report.get("artifacts")
    markdown_path = (
        artifacts.get("history_markdown")
        if isinstance(artifacts, dict) and artifacts.get("history_markdown")
        else str(path.with_suffix(".md"))
    )
    return {
        "id": str(report.get("id") or path.stem),
        "created_at": str(report.get("created_at") or ""),
        "status": str(report.get("status") or "unknown"),
        "mode": str(report.get("mode") or "unknown"),
        "repeat": report.get("repeat"),
        "fix": report.get("fix"),
        "strict": report.get("strict"),
        "require_qemu": report.get("require_qemu"),
        "doctor_status": str(nested_value(report, ("doctor", "status")) or "unknown"),
        "doctor_warnings": doctor_warnings,
        "failed_commands": failed,
        "warning_commands": warned,
        "flaky_commands": nested_value(report, ("summary", "flaky_commands")) or [],
        "recovered_commands": nested_value(report, ("summary", "recovered_commands")) or [],
        "path": str(path),
        "markdown": str(markdown_path),
    }


def read_stress_history(root: Path, *, limit: int) -> dict[str, Any]:
    limit = max(1, limit)
    history_dir = stress_history_dir(root)
    run_ids = stress_history_run_ids(root)
    rows: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for run_id in reversed(run_ids):
        if len(rows) >= limit:
            break
        path = history_dir / f"{run_id}.json"
        try:
            report = read_json_object(path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            unreadable.append({"path": str(path), "error": str(error)})
            continue
        rows.append(stress_history_row(report, path=path))
    return {
        "total_history": len(run_ids),
        "shown": len(rows),
        "limit": limit,
        "runs": rows,
        "unreadable": unreadable,
    }


def render_stress_history(history: dict[str, Any]) -> str:
    lines = [
        f"CyntOX stress history: showing {history.get('shown')} of {history.get('total_history')}",
    ]
    runs = history.get("runs")
    if not isinstance(runs, list) or not runs:
        lines.append("No stress history found yet.")
        return "\n".join(lines)
    for run in runs:
        if not isinstance(run, dict):
            continue
        failed = run.get("failed_commands")
        warned = run.get("warning_commands")
        doctor_warnings = run.get("doctor_warnings")
        bits = [
            str(run.get("status", "unknown")).upper(),
            str(run.get("created_at", "")),
            str(run.get("id", "")),
            f"mode={run.get('mode')}",
            f"repeat={run.get('repeat')}",
            f"doctor={run.get('doctor_status')}",
        ]
        lines.append(" ".join(bit for bit in bits if bit))
        if isinstance(failed, list) and failed:
            lines.append(f"  failed: {', '.join(str(item) for item in failed)}")
        if isinstance(warned, list) and warned:
            lines.append(f"  warned: {', '.join(str(item) for item in warned)}")
        if isinstance(doctor_warnings, list) and doctor_warnings:
            lines.append(f"  doctor warnings: {', '.join(str(item) for item in doctor_warnings)}")
        lines.append(f"  report: {run.get('markdown')}")
    unreadable = history.get("unreadable")
    if isinstance(unreadable, list) and unreadable:
        lines.append(f"Unreadable history files: {len(unreadable)}")
    return "\n".join(lines)


def latest_stress_report_path(root: Path) -> Path:
    return ensure_project_child(root, root / ".oslab" / "cyntox" / "reports" / "stress-latest.json")


def read_latest_stress_report(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = latest_stress_report_path(root)
    if not path.is_file():
        return None, "no stress report found yet"
    try:
        return read_json_object(path), None
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return None, str(error)


def add_next_action(
    actions: list[dict[str, str]],
    *,
    priority: str,
    title: str,
    command: str,
    reason: str,
) -> None:
    key = (priority, title, command)
    existing = {(item.get("priority"), item.get("title"), item.get("command")) for item in actions}
    if key in existing:
        return
    actions.append(
        {
            "priority": priority,
            "title": title,
            "command": command,
            "reason": reason,
        }
    )


def actions_from_doctor_report(doctor_report: dict[str, Any]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    checks = doctor_report.get("checks")
    if not isinstance(checks, list):
        return actions
    for check in checks:
        if not isinstance(check, dict) or check.get("status") == "ok":
            continue
        name = str(check.get("name") or "")
        message = str(check.get("message") or "")
        details_raw = check.get("details")
        details = details_raw if isinstance(details_raw, dict) else {}
        diagnostic = str(details.get("diagnostic") or "")
        remediation = str(details.get("remediation") or "")
        if name == "docker/qemu stress":
            add_next_action(
                actions,
                priority="blocker",
                title="Start Docker Desktop Linux engine for real OS/QEMU stress",
                command=".\\cyntox.cmd stress --require-qemu --fix",
                reason=(
                    remediation
                    or diagnostic
                    or message
                    or "QEMU stress cannot prove anything until Docker is reachable."
                ),
            )
        elif name == "git remote":
            add_next_action(
                actions,
                priority="blocker",
                title="Add the private GitHub remote before pushing",
                command="git remote add origin <private-github-repo-url>",
                reason=message or "Push cannot run until a remote URL exists.",
            )
        elif name in {"qwen settings", "cyntox proxy"}:
            add_next_action(
                actions,
                priority="fix",
                title="Regenerate CyntOX/Qwen settings and restart the proxy",
                command=".\\cyntox.cmd chat",
                reason=message or "Chat startup rewrites settings and refreshes the local proxy.",
            )
        elif name in {"launcher files", "qwen package"}:
            add_next_action(
                actions,
                priority="fix",
                title="Repair local launcher/runtime dependencies",
                command=".\\scripts\\bootstrap.ps1",
                reason=message or "Bootstrap restores local runtime files.",
            )
        else:
            add_next_action(
                actions,
                priority="inspect",
                title=f"Inspect doctor warning: {name}",
                command=".\\cyntox.cmd doctor",
                reason=message or "Doctor reported a non-OK check.",
            )
    return actions


def non_ok_doctor_check_names(doctor_report: dict[str, Any]) -> set[str]:
    checks = doctor_report.get("checks")
    if not isinstance(checks, list):
        return set()
    return {
        str(check.get("name") or "")
        for check in checks
        if isinstance(check, dict) and check.get("status") != "ok"
    }


def actions_from_stress_report(
    stress_report: dict[str, Any] | None, *, docker_qemu_blocked: bool = False
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if not stress_report:
        add_next_action(
            actions,
            priority="verify",
            title="Create the first stress report",
            command=".\\cyntox.cmd stress --fix --rerun-failures 1",
            reason="No latest stress report exists yet.",
        )
        return actions
    commands = stress_report.get("commands")
    if not isinstance(commands, list):
        return actions
    failed = [
        str(command.get("name"))
        for command in commands
        if isinstance(command, dict) and command.get("status") == "fail"
    ]
    warned = [
        str(command.get("name"))
        for command in commands
        if isinstance(command, dict) and command.get("status") == "warn"
    ]
    recovered = nested_value(stress_report, ("summary", "recovered_commands"))
    flaky = nested_value(stress_report, ("summary", "flaky_commands"))
    if failed:
        add_next_action(
            actions,
            priority="fix",
            title="Fix failing stress gates",
            command=".\\cyntox.cmd stress --fix --rerun-failures 1",
            reason=f"Latest stress failed: {', '.join(failed)}.",
        )
    if isinstance(recovered, list) and recovered:
        add_next_action(
            actions,
            priority="verify",
            title="Prove recovered failures are no longer flaky",
            command=".\\cyntox.cmd stress --quick --repeat 5 --skip-qemu --fix",
            reason=f"Recovered on rerun: {', '.join(str(item) for item in recovered)}.",
        )
    if isinstance(flaky, list) and flaky:
        add_next_action(
            actions,
            priority="verify",
            title="Chase flaky stress commands",
            command=".\\cyntox.cmd stress --quick --repeat 5 --skip-qemu --fix",
            reason=f"Status changed across repeats: {', '.join(str(item) for item in flaky)}.",
        )
    if (
        "qemu stress tests" in warned
        and "qemu stress tests" not in failed
        and not docker_qemu_blocked
    ):
        add_next_action(
            actions,
            priority="blocker",
            title="Run required QEMU proof after Docker is available",
            command=".\\cyntox.cmd stress --require-qemu --fix",
            reason="Latest stress only warned on QEMU, so OS stress remains unproven.",
        )
    if not failed and not warned:
        add_next_action(
            actions,
            priority="verify",
            title="Run repeated daily stress",
            command=".\\cyntox.cmd stress --quick --repeat 3 --skip-qemu --fix",
            reason="Latest stress had no failing or warning gates.",
        )
    return actions


def build_next_report(root: Path, *, history_limit: int = 5) -> dict[str, Any]:
    doctor_report = build_doctor_report(root)
    latest_stress, stress_error = read_latest_stress_report(root)
    actions: list[dict[str, str]] = []
    doctor_warnings = non_ok_doctor_check_names(doctor_report)
    for action in actions_from_doctor_report(doctor_report):
        add_next_action(actions, **action)
    for action in actions_from_stress_report(
        latest_stress,
        docker_qemu_blocked="docker/qemu stress" in doctor_warnings,
    ):
        add_next_action(actions, **action)
    add_next_action(
        actions,
        priority="inspect",
        title="Review recent stress trend",
        command=f".\\cyntox.cmd stress history --limit {history_limit}",
        reason="Shows whether recent failures are new, repeated, or already fixed.",
    )
    priority_order = {"blocker": 0, "fix": 1, "verify": 2, "inspect": 3}
    actions.sort(key=lambda item: (priority_order.get(item["priority"], 9), item["title"]))
    history = read_stress_history(root, limit=history_limit)
    return {
        "status": "needs_action" if actions else "ok",
        "doctor_status": doctor_report.get("status"),
        "latest_stress_status": latest_stress.get("status") if latest_stress else None,
        "latest_stress_error": stress_error,
        "actions": actions,
        "history": history,
    }


def render_next_report(report: dict[str, Any]) -> str:
    lines = [
        "CyntOX next actions",
        f"Doctor: {report.get('doctor_status')}  Latest stress: {report.get('latest_stress_status') or report.get('latest_stress_error')}",
        "",
    ]
    actions = report.get("actions")
    if not isinstance(actions, list) or not actions:
        lines.append(
            "No next actions found. Run .\\cyntox.cmd stress --strict --require-qemu --fix for a proof gate."
        )
        return "\n".join(lines)
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            continue
        lines.append(
            f"{index}. [{str(action.get('priority', 'info')).upper()}] {action.get('title')}"
        )
        lines.append(f"   Run: {action.get('command')}")
        lines.append(f"   Why: {action.get('reason')}")
    return "\n".join(lines)


def stress_flaky_commands(command_results: list[dict[str, Any]]) -> list[str]:
    statuses_by_name: dict[str, set[str]] = {}
    for result in command_results:
        name = str(result.get("name") or "")
        if not name:
            continue
        statuses_by_name.setdefault(name, set()).add(str(result.get("status") or "unknown"))
    return sorted(name for name, statuses in statuses_by_name.items() if len(statuses) > 1)


def build_stress_report(
    root: Path,
    *,
    quick: bool = False,
    include_qemu: bool = True,
    fix: bool = False,
    repeat: int = 1,
    require_qemu: bool = False,
    rerun_failures: int = 0,
    strict: bool = False,
) -> dict[str, Any]:
    repeat = max(1, min(repeat, MAX_STRESS_REPEAT))
    rerun_failures = max(0, min(rerun_failures, MAX_STRESS_RERUN_FAILURES))
    doctor_report = build_doctor_report(root)
    command_results: list[dict[str, Any]] = []
    for iteration in range(1, repeat + 1):
        for name, command, timeout_seconds in stress_command_plan(
            root, quick=quick, include_qemu=include_qemu, fix=fix
        ):
            result = run_stress_command(root, name, command, timeout_seconds)
            result["iteration"] = iteration
            enforce_required_qemu(result, require_qemu=require_qemu)
            rerun_failed_stress_command(
                root,
                result,
                timeout_seconds,
                rerun_failures=rerun_failures,
                require_qemu=require_qemu,
            )
            command_results.append(result)
    command_results.append(process_leak_scan_result(root))
    statuses = {str(result.get("status")) for result in command_results}
    flaky_commands = stress_flaky_commands(command_results)
    recovered_commands = sorted(
        {str(result.get("name")) for result in command_results if result.get("recovered_on_rerun")}
    )
    if doctor_report.get("status") == "fail" or "fail" in statuses:
        status = "fail"
    elif (
        doctor_report.get("status") == "warn"
        or "warn" in statuses
        or flaky_commands
        or recovered_commands
    ):
        status = "warn"
    else:
        status = "ok"
    if strict and status == "warn":
        status = "fail"
    return {
        "id": new_job_id(),
        "status": status,
        "root": str(root),
        "created_at": utc_now_iso(),
        "mode": "quick" if quick else "standard",
        "include_qemu": include_qemu,
        "require_qemu": require_qemu,
        "fix": fix,
        "repeat": repeat,
        "rerun_failures": rerun_failures,
        "strict": strict,
        "doctor": doctor_report,
        "commands": command_results,
        "summary": {
            "repeat": repeat,
            "total_commands": len(command_results),
            "failed_commands": sum(
                1 for result in command_results if result.get("status") == "fail"
            ),
            "warning_commands": sum(
                1 for result in command_results if result.get("status") == "warn"
            ),
            "flaky_commands": flaky_commands,
            "recovered_commands": recovered_commands,
        },
    }


def render_stress_report(report: dict[str, Any]) -> str:
    doctor_checks = nested_value(report, ("doctor", "checks"))
    doctor_warnings = (
        [
            str(check.get("name"))
            for check in doctor_checks
            if isinstance(check, dict) and check.get("status") != "ok"
        ]
        if isinstance(doctor_checks, list)
        else []
    )
    lines = [
        f"CyntOX stress: {str(report.get('status', 'unknown')).upper()}",
        f"Mode: {report.get('mode')}  QEMU: {report.get('include_qemu')}  "
        f"Require QEMU: {report.get('require_qemu')}  Fix: {report.get('fix')}  "
        f"Repeat: {report.get('repeat')}  Rerun failures: {report.get('rerun_failures')}  "
        f"Strict: {report.get('strict')}",
        f"Root: {report.get('root')}",
        "",
        f"Doctor: {str(nested_value(report, ('doctor', 'status')) or 'unknown').upper()}",
    ]
    if doctor_warnings:
        lines.append(f"Doctor warnings: {', '.join(doctor_warnings)}")
        doctor_checks = nested_value(report, ("doctor", "checks"))
        if isinstance(doctor_checks, list):
            for check in doctor_checks:
                if not isinstance(check, dict) or check.get("status") == "ok":
                    continue
                details_raw = check.get("details")
                if not isinstance(details_raw, dict):
                    continue
                diagnostic = details_raw.get("diagnostic")
                remediation = details_raw.get("remediation")
                name = check.get("name") or "doctor"
                if diagnostic:
                    lines.append(f"Doctor diagnostic ({name}): {diagnostic}")
                if remediation:
                    lines.append(f"Doctor fix ({name}): {remediation}")
    if report.get("strict") and report.get("status") == "fail":
        lines.append("Strict mode: warnings are treated as failures.")
    for result in report.get("commands", []):
        if not isinstance(result, dict):
            continue
        marker = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}.get(str(result.get("status")), "INFO")
        elapsed = result.get("elapsed_seconds")
        returncode = result.get("returncode")
        iteration = result.get("iteration")
        prefix = f"#{iteration} " if report.get("repeat", 1) != 1 and iteration else ""
        lines.append(f"[{marker}] {prefix}{result.get('name')} rc={returncode} elapsed={elapsed}s")
        diagnostic = stress_result_diagnostic(result)
        if diagnostic:
            lines.append(f"      {diagnostic}")
        leaks = result.get("leaks")
        if isinstance(leaks, list) and leaks:
            leaked_pids = [
                str(leak.get("pid")) for leak in leaks if isinstance(leak, dict) and leak.get("pid")
            ]
            if leaked_pids:
                lines.append(f"      leaked pids: {', '.join(leaked_pids)}")
        reruns = result.get("reruns")
        if isinstance(reruns, list):
            for rerun in reruns:
                if not isinstance(rerun, dict):
                    continue
                rerun_marker = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}.get(
                    str(rerun.get("status")), "INFO"
                )
                lines.append(
                    f"      rerun #{rerun.get('rerun_attempt')} [{rerun_marker}] "
                    f"rc={rerun.get('returncode')} elapsed={rerun.get('elapsed_seconds')}s"
                )
                rerun_diagnostic = stress_result_diagnostic(rerun)
                if rerun_diagnostic:
                    lines.append(f"        {rerun_diagnostic}")
    flaky_commands = nested_value(report, ("summary", "flaky_commands"))
    if isinstance(flaky_commands, list) and flaky_commands:
        lines.extend(["", f"Flaky commands: {', '.join(str(name) for name in flaky_commands)}"])
    recovered_commands = nested_value(report, ("summary", "recovered_commands"))
    if isinstance(recovered_commands, list) and recovered_commands:
        lines.extend(
            [
                "",
                "Recovered on rerun: " + ", ".join(str(name) for name in recovered_commands),
            ]
        )
    retention = report.get("retention")
    if isinstance(retention, dict):
        lines.extend(
            [
                "",
                f"History retained: {retention.get('history_count')} run(s) "
                f"(keep {retention.get('keep_history')})",
            ]
        )
        removed = retention.get("removed")
        if isinstance(removed, list) and removed:
            lines.append(f"Pruned generated reports: {len(removed)} file(s)")
    artifacts = report.get("artifacts")
    if isinstance(artifacts, dict):
        lines.extend(
            [
                "",
                f"JSON: {artifacts.get('json')}",
                f"Markdown: {artifacts.get('markdown')}",
                f"History JSON: {artifacts.get('history_json')}",
                f"History Markdown: {artifacts.get('history_markdown')}",
            ]
        )
    return "\n".join(lines)


def list_jobs(root: Path, jobs_dir: str = DEFAULT_JOBS_DIR) -> list[dict[str, Any]]:
    base = jobs_root(root, jobs_dir)
    if not base.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for job_path in sorted(base.glob("*/job.json")):
        try:
            job = read_json_object(job_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        jobs.append(job)
    return sorted(jobs, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def render_job_summary(job_dir: Path, job: dict[str, Any]) -> str:
    job_id = str(job.get("id") or job_dir.name)
    score = job.get("latest_score") or "?"
    lines = [
        f"CyntOX job {job_id}",
        f"State: {job.get('state')}  Score: {score}",
        (
            f"Kind: {job.get('kind')}  Preset: {job.get('preset')}  "
            f"Mode: {job.get('mode')}  Dry run: {bool(job.get('dry_run'))}"
        ),
        f"Created: {job.get('created_at')}  Updated: {job.get('updated_at')}",
    ]
    if job.get("device") or job.get("service"):
        lines.append(f"Device: {job.get('device') or '-'}  Service: {job.get('service') or '-'}")
    if job.get("worker_pid"):
        lines.append(f"Worker PID: {job.get('worker_pid')}")
    if job.get("retry_of"):
        lines.append(f"Retry of: {job.get('retry_of')}")

    task = cyntox_memory.excerpt(str(job.get("task") or ""), JOB_SHOW_TASK_PREVIEW_LIMIT)
    lines.extend(["", "--- task preview ---", task or "(empty task)"])

    output_path = job_dir / "output.md"
    output = (
        output_path.read_text(encoding="utf-8", errors="replace") if output_path.is_file() else ""
    )
    if output.strip():
        lines.extend(
            [
                "",
                "--- output.md preview ---",
                cyntox_memory.excerpt(output, JOB_SHOW_OUTPUT_PREVIEW_LIMIT),
            ]
        )

    errors_path = job_dir / "errors.log"
    errors = (
        errors_path.read_text(encoding="utf-8", errors="replace") if errors_path.is_file() else ""
    )
    if errors.strip():
        lines.extend(
            [
                "",
                "--- errors.log preview ---",
                cyntox_memory.excerpt(errors, JOB_SHOW_ERROR_PREVIEW_LIMIT),
            ]
        )

    lines.extend(
        [
            "",
            "Artifacts:",
            f"  job: {job_dir / 'job.json'}",
            f"  output: {output_path}",
            f"  verification: {job_dir / 'verification.md'}",
            f"  score: {job_dir / 'score.json'}",
            "Use --json for compact parseable metadata or --json --full for full job metadata.",
        ]
    )
    return "\n".join(lines)


def read_worker_log_tail(job_dir: Path, filename: str) -> str:
    path = job_dir / filename
    if not path.is_file():
        return ""
    try:
        return tail_text(path.read_text(encoding="utf-8", errors="replace"), WORKER_LOG_TAIL_LIMIT)
    except OSError as error:
        return f"[could not read {filename}: {error}]"


def record_stale_worker_evidence(job_dir: Path, pid: int) -> dict[str, Any]:
    stdout_tail = read_worker_log_tail(job_dir, "worker.stdout.log").strip()
    stderr_tail = read_worker_log_tail(job_dir, "worker.stderr.log").strip()
    evidence: dict[str, Any] = {"stale_worker_pid": pid}
    if stdout_tail:
        evidence["worker_stdout_tail"] = stdout_tail
    if stderr_tail:
        evidence["worker_stderr_tail"] = stderr_tail

    verification_lines = [
        "",
        "## Stale worker sweep",
        "",
        f"Detected dead worker PID: {pid}",
        "State changed to failed so the job can be retried from metadata.",
    ]
    if stdout_tail:
        verification_lines.extend(["", "### worker.stdout.log tail", "", stdout_tail])
    if stderr_tail:
        verification_lines.extend(["", "### worker.stderr.log tail", "", stderr_tail])
    append_text(job_dir / "verification.md", "\n".join(verification_lines))

    output_path = job_dir / "output.md"
    existing_output = (
        output_path.read_text(encoding="utf-8", errors="replace").strip()
        if output_path.is_file()
        else ""
    )
    if not existing_output:
        output_path.write_text(
            (
                "CyntOX job worker stopped unexpectedly. "
                "Use `cyntox jobs retry <job-id>` to rerun from saved metadata.\n"
            ),
            encoding="utf-8",
        )
    if stderr_tail:
        append_text(job_dir / "errors.log", f"[stale worker stderr tail]\n{stderr_tail}")
    return evidence


def cmd_jobs(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox jobs")
    parser.add_argument("--jobs-dir", default=DEFAULT_JOBS_DIR)
    subparsers = parser.add_subparsers(dest="command")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("job_id")
    show_parser.add_argument("--json", action="store_true")
    show_parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("job_id", nargs="?")
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("job_id")
    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("job_id")
    subparsers.add_parser("sweep-stale")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--json", action="store_true")
    report_parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    args = parser.parse_args(argv or ["list"])

    if args.command in {None, "list"}:
        jobs = list_jobs(root, args.jobs_dir)
        if getattr(args, "json", False):
            print(terminal_json({"jobs": jobs}, full=getattr(args, "full", False)))
        else:
            for job in jobs:
                score = job.get("latest_score") or "?"
                task = cyntox_memory.excerpt(
                    str(job.get("task") or ""), JOB_LIST_TASK_PREVIEW_LIMIT
                )
                print(f"{job.get('id')} {job.get('state')} score={score} {task}")
        return 0
    if args.command in {"show", "status"} and getattr(args, "job_id", None):
        job_dir, job = load_job(root, args.job_id, args.jobs_dir)
        if getattr(args, "json", False):
            print(
                terminal_json(
                    job, full=getattr(args, "full", False), full_artifact=job_dir / "job.json"
                )
            )
        else:
            print(render_job_summary(job_dir, job))
        return 0
    if args.command == "status":
        jobs = list_jobs(root, args.jobs_dir)
        for job in jobs:
            score = job.get("latest_score") or "?"
            task = cyntox_memory.excerpt(str(job.get("task") or ""), JOB_LIST_TASK_PREVIEW_LIMIT)
            print(f"{job.get('id')} {job.get('state')} score={score} {task}")
        return 0
    if args.command == "resume":
        job_dir, job = load_job(root, args.job_id, args.jobs_dir)
        pid = int(job.get("worker_pid") or 0)
        if job.get("state") == "running" and pid_is_running(pid):
            print(f"Job {args.job_id} is already running as PID {pid}.")
            return 0
        if job.get("state") == "done":
            print(f"Job {args.job_id} is already done.")
            return 0
        set_job_state(job_dir, job, "queued", resumed_at=utc_now_iso())
        pid = start_worker(root, args.job_id, args.jobs_dir)
        print(f"Resumed job {args.job_id} as PID {pid}.")
        return 0
    if args.command == "retry":
        _, original = load_job(root, args.job_id, args.jobs_dir)
        new_id, _, _ = create_job(
            root,
            str(original.get("task") or ""),
            kind=str(original.get("kind") or "task"),
            preset=str(original.get("preset") or cyntox_council.DEFAULT_PRESET),
            mode=str(original.get("mode") or "plan"),
            skills=[str(skill) for skill in original.get("skills", [])],
            device=str(original.get("device")) if original.get("device") else None,
            service=str(original.get("service")) if original.get("service") else None,
            dry_run=bool(original.get("dry_run")),
            save_memory=bool(original.get("save_memory", True)),
            jobs_dir=args.jobs_dir,
            max_wall_time=str(original.get("max_wall_time") or "3m"),
            retry_of=args.job_id,
        )
        pid = start_worker(root, new_id, args.jobs_dir)
        print(f"Retry job {new_id} started as PID {pid}.")
        return 0
    if args.command == "sweep-stale":
        swept = 0
        for listed_job in list_jobs(root, args.jobs_dir):
            if listed_job.get("state") != "running":
                continue
            pid = int(listed_job.get("worker_pid") or listed_job.get("pid") or 0)
            if pid_is_running(pid):
                continue
            listed_job_dir = jobs_root(root, args.jobs_dir) / str(listed_job["id"])
            evidence = record_stale_worker_evidence(listed_job_dir, pid)
            set_job_state(
                listed_job_dir,
                listed_job,
                "failed",
                failure="stale_worker",
                returncode=1,
                swept_at=utc_now_iso(),
                **evidence,
            )
            swept += 1
        print(f"Swept {swept} stale running job(s).")
        return 0
    if args.command == "report":
        return write_jobs_report(
            root,
            jobs_dir=args.jobs_dir,
            as_json=args.json,
            full_json=getattr(args, "full", False),
        )
    parser.error("unknown jobs command")


def write_jobs_report(
    root: Path, *, jobs_dir: str, as_json: bool = False, full_json: bool = False
) -> int:
    jobs = list_jobs(root, jobs_dir)
    scores = [
        float(job["latest_score"])
        for job in jobs
        if isinstance(job.get("latest_score"), int | float)
    ]
    report_dir = ensure_project_child(root, root / ".oslab" / "cyntox" / "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    states = {
        state: sum(1 for job in jobs if job.get("state") == state) for state in sorted(JOB_STATES)
    }
    payload: dict[str, Any] = {
        "created_at": utc_now_iso(),
        "total_jobs": len(jobs),
        "states": states,
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
        "recent_jobs": jobs[:10],
    }
    json_path = report_dir / "latest-report.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown = [
        "# CyntOX Jobs Report",
        "",
        f"Generated: {payload['created_at']}",
        f"Total jobs: {payload['total_jobs']}",
        f"Average score: {payload['average_score']}",
        "",
        "## States",
        "",
        *[f"- {state}: {count}" for state, count in states.items()],
    ]
    (report_dir / "latest-report.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    if as_json:
        print(terminal_json(payload, full=full_json, full_artifact=json_path))
    else:
        print(report_dir / "latest-report.md")
    return 0


def cmd_skills(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox skills")
    parser.add_argument("--skills-dir", default="skills")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    archive = subparsers.add_parser("archive-unused")
    archive.add_argument("--days", type=int, required=True)
    archive.add_argument("--dry-run", action="store_true")
    use = subparsers.add_parser("use")
    use.add_argument("skill")
    use.add_argument("task", nargs="+")
    use.add_argument("--dry-run", action="store_true")
    use.add_argument("--foreground", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "list":
        registry = cyntox_council.sync_skill_registry(root, args.skills_dir)
        if args.json:
            print(
                terminal_json(
                    registry,
                    full=getattr(args, "full", False),
                    full_artifact=root / args.skills_dir / ".registry.json",
                )
            )
        else:
            for name, entry in sorted(registry.get("skills", {}).items()):
                if isinstance(entry, dict):
                    print(
                        f"{name} use_count={entry.get('use_count', 0)} "
                        f"avg_score={entry.get('score_average', '?')} archived={entry.get('archived_at')}"
                    )
        return 0
    if args.command == "archive-unused":
        archived = cyntox_council.archive_unused_skills(
            root,
            args.skills_dir,
            args.days,
            dry_run=args.dry_run,
        )
        for item in archived:
            print(
                f"{'Would archive' if args.dry_run else 'Archived'}: {item['name']} -> {item['target']}"
            )
        if archived and not args.dry_run:
            cyntox_council.mirror_skill_archive_notes(
                root,
                archived,
                source="cyntox skills archive-unused",
            )
        if not archived:
            print("No unused skills matched the threshold.")
        return 0
    if args.command == "use":
        task = " ".join(args.task).strip()
        job_id, _, _ = create_job(
            root,
            task,
            skills=[args.skill],
            dry_run=args.dry_run,
            jobs_dir=DEFAULT_JOBS_DIR,
        )
        if args.foreground:
            code = run_job(root, job_id)
            print_foreground_result(root, job_id, code)
            return code
        pid = start_worker(root, job_id)
        print(f"Queued cyntox job {job_id} as PID {pid}.")
        print(f"Inspect with: .\\cyntox.cmd jobs show {job_id}")
        return 0
    return 2


def cmd_devices(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox devices")
    parser.add_argument("--devices-file", default=DEFAULT_DEVICES_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    show = subparsers.add_parser("show")
    show.add_argument("device")
    show.add_argument("--json", action="store_true")
    show.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("device")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    args = parser.parse_args(argv)

    devices = load_devices(root, args.devices_file)
    if args.command == "list":
        if args.json:
            print(
                terminal_json(
                    {"devices": devices},
                    full=getattr(args, "full", False),
                    full_artifact=root / args.devices_file,
                )
            )
        else:
            for name, device in sorted(devices.items()):
                if not isinstance(device, dict):
                    continue
                status, issues = device_status(device)
                print(
                    f"{name} type={device.get('type')} connection={device.get('connection')} "
                    f"status={status} issues={','.join(issues) if issues else '-'}"
                )
        return 0
    device = resolve_device(root, args.device, args.devices_file)
    status, issues = device_status(device)
    if args.command == "show":
        payload = {"name": args.device, "status": status, "issues": issues, "device": device}
        if args.json:
            print(
                terminal_json(
                    payload,
                    full=getattr(args, "full", False),
                    full_artifact=root / args.devices_file,
                )
            )
        else:
            print(render_device_summary(args.device, device, status=status, issues=issues))
        return 0
    if args.command == "doctor":
        payload = {"name": args.device, "status": status, "issues": issues, "device": device}
        if args.json:
            print(
                terminal_json(
                    payload,
                    full=getattr(args, "full", False),
                    full_artifact=root / args.devices_file,
                )
            )
        else:
            print(f"{args.device}: {status}")
            for issue in issues:
                print(f"- {issue}")
        return 0
    return 2


def enqueue_task(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox")
    parser.add_argument("task", nargs="+")
    parser.add_argument(
        "--preset", choices=tuple(cyntox_council.PRESETS), default=cyntox_council.DEFAULT_PRESET
    )
    parser.add_argument("--mode", choices=("plan", "implement"), default="plan")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--max-wall-time", default="3m")
    args = parser.parse_args(argv)
    task = " ".join(args.task).strip()
    job_id, _, _ = create_job(
        root,
        task,
        preset=args.preset,
        mode=args.mode,
        dry_run=args.dry_run,
        max_wall_time=args.max_wall_time,
    )
    if args.foreground:
        code = run_job(root, job_id)
        print_foreground_result(root, job_id, code)
        return code
    pid = start_worker(root, job_id)
    print(f"Queued cyntox job {job_id} as PID {pid}.")
    print(f"Inspect with: .\\cyntox.cmd jobs show {job_id}")
    return 0


def cmd_run_on(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox run-on")
    parser.add_argument("device")
    parser.add_argument("task", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--max-wall-time", default="3m")
    args = parser.parse_args(argv)
    task = " ".join(args.task).strip()
    try:
        device = resolve_device(root, args.device)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    status, issues = device_status(device)
    requires_write = task_needs_write(task)
    if status not in {"ready", "limited"} and not args.dry_run:
        reason = (
            f"Device {args.device} is not ready for execution: {status} "
            f"({', '.join(issues) if issues else 'no details'}). Use --dry-run or configure a real channel."
        )
        job_id, _, _ = create_job(
            root,
            task,
            kind="run-on",
            mode="plan",
            skills=infer_skills(task) or ["pc-admin"],
            device=args.device,
            dry_run=True,
            needs_input_reason=reason,
            max_wall_time=args.max_wall_time,
        )
        print(f"Created needs_input job {job_id}: {reason}")
        return 2
    job_id, _, job = create_job(
        root,
        task,
        kind="run-on",
        mode="plan",
        skills=infer_skills(task) or ["pc-admin"],
        device=args.device,
        dry_run=args.dry_run,
        requires_approval=requires_write,
        max_wall_time=args.max_wall_time,
    )
    if requires_write and not args.dry_run and not bool(device.get("approved_writes")):
        reason = f"Approval required before write/install on {args.device}."
        job["needs_input_reason"] = reason
        (jobs_root(root) / job_id / "output.md").write_text(reason + "\n", encoding="utf-8")
        set_job_state(jobs_root(root) / job_id, job, "needs_input", needs_input_reason=reason)
        print(f"Created needs_input job {job_id}: {reason}")
        return 2
    if args.foreground:
        code = run_job(root, job_id)
        print_foreground_result(root, job_id, code)
        return code
    pid = start_worker(root, job_id)
    print(f"Queued cyntox device job {job_id} as PID {pid}.")
    print(f"Inspect with: .\\cyntox.cmd jobs show {job_id}")
    return 0


def cmd_setup(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox setup")
    parser.add_argument("service")
    parser.add_argument("--target", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--max-wall-time", default="3m")
    args = parser.parse_args(argv)
    try:
        device = resolve_device(root, args.target)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    service = args.service.lower()
    forced_dry_run = True
    task = (
        f"Set up {service} on {args.target}. Produce a dry-run plan first, with commands, "
        "approval points, verification checks, and rollback notes."
    )
    if device_allowed_service_match(device, service) is None:
        allowed = device.get("allowed_services")
        reason = (
            f"Service {service!r} is not allowed for target {args.target}. "
            f"Allowed services: {', '.join(str(item) for item in allowed) if isinstance(allowed, list) and allowed else 'none'}."
        )
        job_id, _, _ = create_job(
            root,
            task,
            kind="setup",
            mode="plan",
            skills=SERVICE_SKILLS.get(service, ["pc-admin"]),
            device=args.target,
            service=service,
            dry_run=True,
            requires_approval=True,
            needs_input_reason=reason,
            max_wall_time=args.max_wall_time,
        )
        print(f"Created needs_input job {job_id}: {reason}")
        return 2
    job_id, _, _ = create_job(
        root,
        task,
        kind="setup",
        mode="plan",
        skills=SERVICE_SKILLS.get(service, ["pc-admin"]),
        device=args.target,
        service=service,
        dry_run=forced_dry_run or args.dry_run,
        requires_approval=True,
        max_wall_time=args.max_wall_time,
    )
    if args.foreground:
        code = run_job(root, job_id)
        print_foreground_result(root, job_id, code)
        return code
    pid = start_worker(root, job_id)
    print(f"Queued cyntox setup dry-run job {job_id} as PID {pid}.")
    print(f"Inspect with: .\\cyntox.cmd jobs show {job_id}")
    return 0


def cmd_memory(argv: list[str]) -> int:
    return cyntox_memory.main(argv)


def cmd_privacy(argv: list[str]) -> int:
    return cyntox_privacy.main(argv)


def cmd_vault(root: Path, argv: list[str]) -> int:
    if not argv or argv == ["path"]:
        print(cyntox_memory.vault_root(root))
        return 0
    return cmd_memory(argv)


def cmd_chat(root: Path, argv: list[str]) -> int:
    launcher = root / "qwen-code.ps1"
    completed = subprocess.run(  # noqa: S603
        [
            find_powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            *argv,
        ],
        cwd=root,
        check=False,
    )
    return completed.returncode


def cmd_benchmark(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox benchmark")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preset", choices=tuple(cyntox_council.PRESETS), default="max")
    parser.add_argument("--max-wall-time", default="3m")
    args = parser.parse_args(argv)
    council_args = [
        "--benchmark",
        "--preset",
        args.preset,
        "--pass-threshold",
        str(DEFAULT_QUALITY_TARGET),
        "--max-wall-time",
        args.max_wall_time,
        "--out-dir",
        ".oslab/cyntox/benchmarks",
    ]
    if args.dry_run:
        council_args.append("--dry-run")
    return cyntox_council.main(council_args)


def cmd_doctor(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox doctor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    args = parser.parse_args(argv)
    report = build_doctor_report(root)
    if args.json:
        print(terminal_json(report, full=args.full))
    else:
        print(render_doctor_report(report))
    return 0 if report["status"] != "fail" else 1


def cmd_next(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox next")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    parser.add_argument("--history-limit", type=int, default=5)
    args = parser.parse_args(argv)
    if args.history_limit < 1:
        parser.error("--history-limit must be at least 1")
    report = build_next_report(root, history_limit=args.history_limit)
    if args.json:
        print(terminal_json(report, full=args.full))
    else:
        print(render_next_report(report))
    return 0


def cmd_stress(root: Path, argv: list[str]) -> int:
    if argv and argv[0].lower() == "history":
        parser = argparse.ArgumentParser(prog="cyntox stress history")
        parser.add_argument("--json", action="store_true")
        parser.add_argument(
            "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
        )
        parser.add_argument("--limit", type=int, default=10)
        args = parser.parse_args(argv[1:])
        if args.limit < 1:
            parser.error("--limit must be at least 1")
        history = read_stress_history(root, limit=args.limit)
        if args.json:
            print(terminal_json(history, full=args.full))
        else:
            print(render_stress_history(history))
        return 0

    parser = argparse.ArgumentParser(prog="cyntox stress")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--full", action="store_true", help="print full JSON instead of compact terminal JSON"
    )
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--skip-qemu", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail if any stress check warns")
    parser.add_argument(
        "--require-qemu",
        action="store_true",
        help="fail if the QEMU stress gate is skipped or only warns",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=f"repeat the stress command plan 1-{MAX_STRESS_REPEAT} times to catch flakes",
    )
    parser.add_argument(
        "--rerun-failures",
        type=int,
        default=0,
        help=(
            "rerun each failed gate up to this many times "
            f"(0-{MAX_STRESS_RERUN_FAILURES}) to classify flakes"
        ),
    )
    parser.add_argument(
        "--keep-history",
        type=int,
        default=DEFAULT_STRESS_HISTORY_KEEP,
        help=f"number of timestamped stress reports to keep when pruning (default {DEFAULT_STRESS_HISTORY_KEEP})",
    )
    parser.add_argument(
        "--prune-history",
        dest="prune_history",
        action="store_true",
        default=True,
        help="delete old generated timestamped stress reports beyond --keep-history (default)",
    )
    parser.add_argument(
        "--no-prune-history",
        dest="prune_history",
        action="store_false",
        help="keep all generated timestamped stress reports for this run",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="apply safe local formatter cleanup before running checks",
    )
    args = parser.parse_args(argv)
    if args.skip_qemu and args.require_qemu:
        parser.error("--require-qemu cannot be combined with --skip-qemu")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.repeat > MAX_STRESS_REPEAT:
        parser.error(f"--repeat must be at most {MAX_STRESS_REPEAT}")
    if args.rerun_failures < 0:
        parser.error("--rerun-failures must be at least 0")
    if args.rerun_failures > MAX_STRESS_RERUN_FAILURES:
        parser.error(f"--rerun-failures must be at most {MAX_STRESS_RERUN_FAILURES}")
    if args.keep_history < 1:
        parser.error("--keep-history must be at least 1")
    report = build_stress_report(
        root,
        quick=args.quick,
        include_qemu=not args.skip_qemu,
        fix=args.fix,
        repeat=args.repeat,
        require_qemu=args.require_qemu,
        rerun_failures=args.rerun_failures,
        strict=args.strict,
    )
    write_stress_artifacts(root, report)
    removed_history: list[str] = []
    if args.prune_history:
        removed_history = prune_stress_history(root, keep=args.keep_history)
    report["retention"] = {
        "history_count": len(stress_history_run_ids(root)),
        "keep_history": args.keep_history,
        "prune_history": args.prune_history,
        "removed": removed_history,
    }
    write_stress_artifacts(root, report)
    if args.json:
        full_artifact = (
            Path(str(report["artifacts"]["json"]))
            if isinstance(report.get("artifacts"), dict) and report["artifacts"].get("json")
            else None
        )
        print(terminal_json(report, full=args.full, full_artifact=full_artifact))
    else:
        print(render_stress_report(report))
    return 0 if report["status"] != "fail" else 1


def print_main_help() -> None:
    print(
        "\n".join(
            [
                "usage: cyntox [command] [args]",
                "",
                "CyntOX daily-use commands:",
                '  cyntox "task"                         queue a local-first council job',
                "  cyntox chat                           open interactive CyntOX/Qwen Code",
                "  cyntox jobs list|show|resume|retry    inspect and recover jobs",
                "  cyntox skills list|use|archive-unused track reusable skills",
                "  cyntox memory add|search|sync|extract manage vault/RAG memory",
                "  cyntox vault path                     print the Obsidian vault path",
                "  cyntox devices list|show|doctor       inspect device registry",
                '  cyntox run-on <device> "task"         device-scoped dry-run/safe job',
                "  cyntox setup <service> --target <id>  service setup, dry-run first",
                "  cyntox privacy policy|scan|check-url  privacy/injection guard",
                "  cyntox next                           show current next actions",
                "  cyntox doctor                         preflight limits/proxy/terminal checks",
                "  cyntox stress [--fix] [--repeat N]    run repeated quality/stress gates",
                "  cyntox stress history                 show recent stress report trend",
                "  cyntox stress --rerun-failures N      rerun failed gates to classify flakes",
                "  cyntox stress --strict                fail on any warning",
                "  cyntox stress --require-qemu          fail unless OS/QEMU stress really runs",
                "  cyntox stress --no-prune-history      keep all generated stress reports",
                "  cyntox benchmark --dry-run            run quality benchmark plan",
                "",
                "Defaults: local-first, internet off, remote writes blocked until approved.",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    root = project_root()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_chat(root, [])

    command = args[0].lower()
    tail = args[1:]
    if command in {"-h", "--help", "help"}:
        print_main_help()
        return 0
    if command == "_worker":
        parser = argparse.ArgumentParser(prog="cyntox _worker")
        parser.add_argument("job_id")
        parser.add_argument("--jobs-dir", default=DEFAULT_JOBS_DIR)
        worker_args = parser.parse_args(tail)
        return run_job(root, worker_args.job_id, worker_args.jobs_dir)
    if command == "chat":
        return cmd_chat(root, tail)
    if command == "council":
        return cyntox_council.main(tail)
    if command == "jobs":
        return cmd_jobs(root, tail)
    if command == "skills":
        return cmd_skills(root, tail)
    if command == "memory":
        return cmd_memory(tail)
    if command == "vault":
        return cmd_vault(root, tail)
    if command == "devices":
        return cmd_devices(root, tail)
    if command == "run-on":
        return cmd_run_on(root, tail)
    if command == "setup":
        return cmd_setup(root, tail)
    if command == "privacy" or command == "security":
        return cmd_privacy(tail)
    if command == "doctor":
        return cmd_doctor(root, tail)
    if command == "next":
        return cmd_next(root, tail)
    if command == "stress":
        return cmd_stress(root, tail)
    if command == "benchmark":
        return cmd_benchmark(root, tail)
    return enqueue_task(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
