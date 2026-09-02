from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

try:
    from scripts import cyntox_council, cyntox_memory, cyntox_privacy
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    import cyntox_council  # type: ignore[no-redef]
    import cyntox_memory  # type: ignore[no-redef]
    import cyntox_privacy  # type: ignore[no-redef]


DEFAULT_JOBS_DIR = ".oslab/cyntox/jobs"
DEFAULT_DEVICES_FILE = "devices.toml"
DEFAULT_QUALITY_TARGET = 9.0
DEFAULT_MAX_RETRIES = 1
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
    re.compile(r"\bformat\b", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/(?:s|q)\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\breboot\b", re.IGNORECASE),
    re.compile(r"\bpublic\s+ip\b", re.IGNORECASE),
)
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


def load_job(root: Path, job_id: str, jobs_dir: str = DEFAULT_JOBS_DIR) -> tuple[Path, dict[str, Any]]:
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
            word in tokens if len(word) <= 3 and " " not in word else word in text
            for word in words
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
        cyntox_council.slugify_skill_name(name) for name in (skills or infer_skills(task, service=service))
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
    if connection == "ssh" and not device.get("host"):
        issues.append("ssh-host-missing")
    if connection in {"ssh", "smb", "api", "rdp"} and not configured:
        return "needs_config", issues
    if issues:
        return "limited", issues
    return "ready", issues


def resolve_device(root: Path, name: str, devices_file: str = DEFAULT_DEVICES_FILE) -> dict[str, Any]:
    devices = load_devices(root, devices_file)
    device = devices.get(name)
    if not isinstance(device, dict):
        known = ", ".join(sorted(devices)) or "none"
        raise ValueError(f"Unknown device: {name}. Known devices: {known}")
    return device


def planned_device_commands(kind: str, task: str, device_name: str, device: dict[str, Any]) -> list[str]:
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


def find_powershell() -> str:
    for candidate in ("pwsh.exe", "powershell.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError("PowerShell was not found.")


def run_local_readonly_command(root: Path, command: str) -> subprocess.CompletedProcess[str]:
    shell = find_powershell()
    completed = subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-Command", command],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
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
    host = str(device.get("host") or "")
    user = str(device.get("user") or "")
    target = f"{user}@{host}" if user else host
    completed = subprocess.run(  # noqa: S603
        [ssh, target, command],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
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
        if task_needs_write(command):
            outputs.append(f"SKIPPED write-like command: {command}")
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
        return {"run_dir": str(run_dir), "overall": None, "scorecard": None, "passed_threshold": False}
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
        if denied:
            raise ValueError(f"Task matches denied device/action pattern: {denied}")

        device_name = job.get("device")
        service = job.get("service")
        if isinstance(device_name, str) and device_name:
            device = resolve_device(root, device_name)
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
            requires_write = task_needs_write(str(job.get("task") or ""), service=str(service) if service else None)
            if requires_write and not bool(job.get("dry_run")) and not bool(device.get("approved_writes")):
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
            text=True,
            capture_output=True,
            check=False,
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
        (job_dir / "verification.md").write_text("\n".join(verification).rstrip() + "\n", encoding="utf-8")

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
                append_jsonl(job_dir / "events.jsonl", {"event": "memory_extracted", "note": str(note)})
            except ValueError as error:
                append_jsonl(job_dir / "events.jsonl", {"event": "memory_skipped", "reason": str(error)})
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
        text=True,
        capture_output=True,
        check=False,
    )
    return str(pid) in completed.stdout


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


def cmd_jobs(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox jobs")
    parser.add_argument("--jobs-dir", default=DEFAULT_JOBS_DIR)
    subparsers = parser.add_subparsers(dest="command")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("job_id")
    show_parser.add_argument("--json", action="store_true")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("job_id", nargs="?")
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("job_id")
    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("job_id")
    subparsers.add_parser("sweep-stale")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv or ["list"])

    if args.command in {None, "list"}:
        jobs = list_jobs(root, args.jobs_dir)
        if getattr(args, "json", False):
            print(json.dumps({"jobs": jobs}, indent=2))
        else:
            for job in jobs:
                score = job.get("latest_score") or "?"
                print(f"{job.get('id')} {job.get('state')} score={score} {job.get('task')}")
        return 0
    if args.command in {"show", "status"} and getattr(args, "job_id", None):
        job_dir, job = load_job(root, args.job_id, args.jobs_dir)
        if getattr(args, "json", False):
            print(json.dumps(job, indent=2))
        else:
            output = (job_dir / "output.md").read_text(encoding="utf-8", errors="replace")
            print(json.dumps(job, indent=2))
            if output.strip():
                print("\n--- output.md ---")
                print(cyntox_memory.excerpt(output, 1600))
        return 0
    if args.command == "status":
        jobs = list_jobs(root, args.jobs_dir)
        for job in jobs:
            score = job.get("latest_score") or "?"
            print(f"{job.get('id')} {job.get('state')} score={score} {job.get('task')}")
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
            set_job_state(
                listed_job_dir,
                listed_job,
                "failed",
                failure="stale_worker",
                returncode=1,
                swept_at=utc_now_iso(),
            )
            swept += 1
        print(f"Swept {swept} stale running job(s).")
        return 0
    if args.command == "report":
        return write_jobs_report(root, jobs_dir=args.jobs_dir, as_json=args.json)
    parser.error("unknown jobs command")
    return 2


def write_jobs_report(root: Path, *, jobs_dir: str, as_json: bool = False) -> int:
    jobs = list_jobs(root, jobs_dir)
    scores = [
        float(job["latest_score"])
        for job in jobs
        if isinstance(job.get("latest_score"), int | float)
    ]
    report_dir = ensure_project_child(root, root / ".oslab" / "cyntox" / "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": utc_now_iso(),
        "total_jobs": len(jobs),
        "states": {state: sum(1 for job in jobs if job.get("state") == state) for state in sorted(JOB_STATES)},
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
        "recent_jobs": jobs[:10],
    }
    (report_dir / "latest-report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown = [
        "# CyntOX Jobs Report",
        "",
        f"Generated: {payload['created_at']}",
        f"Total jobs: {payload['total_jobs']}",
        f"Average score: {payload['average_score']}",
        "",
        "## States",
        "",
        *[f"- {state}: {count}" for state, count in payload["states"].items()],
    ]
    (report_dir / "latest-report.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(report_dir / "latest-report.md")
    return 0


def cmd_skills(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox skills")
    parser.add_argument("--skills-dir", default="skills")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
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
            print(json.dumps(registry, indent=2))
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
            print(f"{'Would archive' if args.dry_run else 'Archived'}: {item['name']} -> {item['target']}")
            if not args.dry_run:
                cyntox_memory.write_note(
                    root,
                    note_type="skill",
                    title=f"Skill archived: {item['name']}",
                    content=f"Skill {item['name']} archived from {item['source']} to {item['target']}.",
                    tags=["cyntox/skill-lifecycle"],
                    source="cyntox skills archive-unused",
                    confidence=0.9,
                )
        if archived and not args.dry_run:
            cyntox_memory.sync_vault(root)
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
            print(f"Job {job_id} finished with code {code}.")
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
    show = subparsers.add_parser("show")
    show.add_argument("device")
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("device")
    doctor.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    devices = load_devices(root, args.devices_file)
    if args.command == "list":
        if args.json:
            print(json.dumps({"devices": devices}, indent=2))
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
        print(json.dumps({"name": args.device, "status": status, "issues": issues, **device}, indent=2))
        return 0
    if args.command == "doctor":
        payload = {"name": args.device, "status": status, "issues": issues, "device": device}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"{args.device}: {status}")
            for issue in issues:
                print(f"- {issue}")
        return 0
    return 2


def enqueue_task(root: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cyntox")
    parser.add_argument("task", nargs="+")
    parser.add_argument("--preset", choices=tuple(cyntox_council.PRESETS), default=cyntox_council.DEFAULT_PRESET)
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
        print(f"Job {job_id} finished with code {code}.")
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
        print(f"Job {job_id} finished with code {code}.")
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
        resolve_device(root, args.target)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    service = args.service.lower()
    forced_dry_run = True
    task = (
        f"Set up {service} on {args.target}. Produce a dry-run plan first, with commands, "
        "approval points, verification checks, and rollback notes."
    )
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
        print(f"Job {job_id} finished with code {code}.")
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


def main(argv: list[str] | None = None) -> int:
    root = project_root()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_chat(root, [])

    command = args[0].lower()
    tail = args[1:]
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
    if command == "benchmark":
        return cmd_benchmark(root, tail)
    return enqueue_task(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
