from __future__ import annotations

import difflib
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from oslab.artifacts import ArtifactStore
from oslab.config import LabConfig
from oslab.database import LabDatabase
from oslab.schemas import utc_now
from oslab.security.adapters import adapter_inventory
from oslab.security.catalog import build_catalog
from oslab.security.models import (
    AuditProfile,
    AuditRun,
    AuditState,
    FindingStatus,
    PatchCandidate,
    SecurityFinding,
)
from oslab.security.reporting import write_reports
from oslab.security.scanner import discover_files, scan_repository
from oslab.security.store import SecurityAuditStore

STAGES = (
    "reconnaissance",
    "specialist_hunt",
    "adversarial_validation",
    "reachability",
    "deduplication",
    "patching",
    "reporting",
)
PROFILE_LIMITS = {
    AuditProfile.QUICK: 2_000,
    AuditProfile.STANDARD: 10_000,
    AuditProfile.DEEP: 50_000,
}


def audit_plan(repository: Path, profile: AuditProfile, base: str = "HEAD") -> dict[str, Any]:
    root = _validate_repository(repository)
    commit = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    dirty = _git(root, "status", "--porcelain=v1")
    readiness = "blocked-dirty-worktree" if dirty and profile != AuditProfile.QUICK else "ready"
    return {
        "repository": str(root),
        "base_commit": commit,
        "dirty": bool(dirty),
        "profile": profile,
        "readiness": readiness,
        "stages": list(STAGES if profile != AuditProfile.QUICK else STAGES[:5] + STAGES[-1:]),
        "max_files": PROFILE_LIMITS[profile],
        "network": "disabled",
        "source_checkout_writes": "forbidden",
        "patches": "disposable-worktrees" if profile != AuditProfile.QUICK else "disabled",
        "adapters": adapter_inventory(),
    }


def create_audit(
    config: LabConfig,
    repository: Path,
    profile: AuditProfile,
    base: str = "HEAD",
) -> AuditRun:
    plan = audit_plan(repository, profile, base)
    if plan["readiness"] != "ready":
        raise ValueError("standard and deep audits require a clean source worktree")
    root = Path(str(plan["repository"]))
    audit = AuditRun(
        id=f"audit-{uuid4().hex[:16]}",
        repository=str(root),
        repository_hash=hashlib.sha256(str(root).encode()).hexdigest(),
        base_commit=str(plan["base_commit"]),
        dirty_state_hash=hashlib.sha256(
            _git(root, "status", "--porcelain=v1").encode()
        ).hexdigest(),
        profile=profile,
        stages=list(plan["stages"]),
    )
    SecurityAuditStore(LabDatabase(config.runtime_root / "oslab.sqlite3")).create(audit)
    return audit


def run_audit(config: LabConfig, audit_id: str) -> AuditRun:
    store = SecurityAuditStore(LabDatabase(config.runtime_root / "oslab.sqlite3"))
    audit = store.get(audit_id)
    if audit.state == AuditState.COMPLETE:
        return audit
    repository = Path(audit.repository)
    _assert_source_identity(repository, audit)
    store.set_state(audit_id, AuditState.RUNNING)
    try:
        reconnaissance = _stage(
            store,
            audit,
            "reconnaissance",
            lambda: _reconnaissance(repository, PROFILE_LIMITS[audit.profile]),
        )
        finding_payloads = _stage(
            store,
            audit,
            "specialist_hunt",
            lambda: {
                "findings": [
                    finding.model_dump(mode="json")
                    for finding in scan_repository(
                        repository, audit.id, max_files=PROFILE_LIMITS[audit.profile]
                    )
                ],
                "reconnaissance": reconnaissance,
            },
        )
        findings = [
            SecurityFinding.model_validate(row) for row in finding_payloads.get("findings", [])
        ]
        _stage(
            store,
            audit,
            "adversarial_validation",
            lambda: {
                "accepted": sum(1 for row in findings if row.status == FindingStatus.CONFIRMED),
                "needs_review": sum(
                    1 for row in findings if row.status == FindingStatus.NEEDS_REVIEW
                ),
                "rejected": sum(1 for row in findings if row.status == FindingStatus.REJECTED),
            },
        )
        _stage(
            store,
            audit,
            "reachability",
            lambda: {
                "traced": sum(1 for row in findings if row.status != FindingStatus.REJECTED),
                "policy": "confirmed requires deterministic sink evidence and adversarial review",
            },
        )
        findings = _deduplicate(findings)
        _stage(
            store,
            audit,
            "deduplication",
            lambda: {
                "unique": len(findings),
                "fingerprints": [row.fingerprint for row in findings],
            },
        )
        if audit.profile != AuditProfile.QUICK:
            patch_payload = _stage(
                store,
                audit,
                "patching",
                lambda: _generate_patches(config, audit, findings),
            )
            patches = {
                str(row["fingerprint"]): PatchCandidate.model_validate(row["patch"])
                for row in patch_payload.get("patches", [])
            }
            for finding in findings:
                finding.patch = patches.get(finding.fingerprint)
        for finding in findings:
            store.record_finding(finding)
        report_root = config.artifacts_root / "security" / audit.id
        audit.state = AuditState.COMPLETE
        audit.report_paths = {
            "json": str((report_root / "findings.json").resolve()),
            "markdown": str((report_root / "REPORT.md").resolve()),
            "sarif": str((report_root / "findings.sarif").resolve()),
        }
        report_paths = _stage(
            store,
            audit,
            "reporting",
            lambda: _write_and_store_reports(config, audit, findings),
        )
        audit.report_paths = {key: str(report_paths[key]) for key in ("json", "markdown", "sarif")}
        audit.updated_at = utc_now()
        store.set_state(audit_id, AuditState.COMPLETE)
        completed = store.get(audit_id)
        completed.report_paths = audit.report_paths
        completed.updated_at = utc_now()
        _persist_audit_config(store.database, completed)
        return completed
    except BaseException as exc:
        state = (
            AuditState.CANCELLED
            if isinstance(exc, KeyboardInterrupt | SystemExit)
            else AuditState.FAILED
        )
        store.set_state(audit_id, state, error=f"{type(exc).__name__}: {exc}")
        raise


def _stage(
    store: SecurityAuditStore,
    audit: AuditRun,
    name: str,
    action: Any,
) -> dict[str, Any]:
    prior = store.stage(audit.id, name)
    if prior is not None and prior["state"] == "complete":
        payload = prior["payload"]
        return payload if isinstance(payload, dict) else {}
    store.checkpoint(audit.id, name, "running", {})
    payload = action()
    if not isinstance(payload, dict):
        raise TypeError(f"audit stage {name} did not return an object")
    store.checkpoint(audit.id, name, "complete", payload)
    return payload


def _reconnaissance(repository: Path, max_files: int) -> dict[str, Any]:
    files = discover_files(repository, max_files=max_files)
    languages: dict[str, int] = {}
    for path in files:
        languages[path.suffix.lower()] = languages.get(path.suffix.lower(), 0) + 1
    return {
        "files_considered": len(files),
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        "manifests": [
            name
            for name in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml")
            if (repository / name).is_file()
        ],
    }


def _deduplicate(findings: list[SecurityFinding]) -> list[SecurityFinding]:
    unique: dict[str, SecurityFinding] = {}
    for finding in findings:
        existing = unique.get(finding.fingerprint)
        if existing is None or finding.confidence > existing.confidence:
            unique[finding.fingerprint] = finding
    return sorted(unique.values(), key=lambda row: (str(row.severity), row.locations[0].path))


def _generate_patches(
    config: LabConfig, audit: AuditRun, findings: list[SecurityFinding]
) -> dict[str, Any]:
    repository = Path(audit.repository)
    patches: list[dict[str, Any]] = []
    for finding in findings:
        if finding.status != FindingStatus.CONFIRMED or finding.rule_id != "PY-YAML-001":
            continue
        location = finding.locations[0]
        source = repository / location.path
        original = source.read_text(encoding="utf-8")
        changed_lines = original.splitlines(keepends=True)
        index = location.line - 1
        if index >= len(changed_lines) or "yaml.load(" not in changed_lines[index]:
            continue
        changed_lines[index] = changed_lines[index].replace("yaml.load(", "yaml.safe_load(", 1)
        changed = "".join(changed_lines)
        worktree = config.runtime_root / "cyntox" / "audits" / audit.id / "worktrees" / finding.id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repository, "worktree", "add", "--detach", str(worktree), audit.base_commit)
        target = worktree / location.path
        target.write_text(changed, encoding="utf-8", newline="")
        compile_result = subprocess.run(  # noqa: S603 - current interpreter only parses file
            [sys.executable, "-m", "py_compile", str(target)],
            cwd=worktree,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                changed.splitlines(keepends=True),
                fromfile=f"a/{location.path}",
                tofile=f"b/{location.path}",
            )
        )
        targeted = "yaml.load removed from matched line"
        regression = (
            "python syntax compilation passed" if compile_result.returncode == 0 else "failed"
        )
        patch = PatchCandidate(
            diff=diff,
            diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
            worktree=str(worktree),
            targeted_check=targeted,
            regression_check=regression,
            verified=bool(diff) and compile_result.returncode == 0,
        )
        patches.append({"fingerprint": finding.fingerprint, "patch": patch.model_dump(mode="json")})
        ArtifactStore(config.artifacts_root).put_json(
            patch.model_dump(mode="json"), f"security-patch-{finding.id}.json"
        )
    return {"patches": patches}


def _validate_repository(repository: Path) -> Path:
    root = repository.resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ValueError("audit target must be an existing Git working tree")
    return root


def _assert_source_identity(repository: Path, audit: AuditRun) -> None:
    if _git(repository, "rev-parse", "HEAD") != audit.base_commit:
        raise RuntimeError("repository HEAD changed after the audit was planned")
    dirty_hash = hashlib.sha256(_git(repository, "status", "--porcelain=v1").encode()).hexdigest()
    if dirty_hash != audit.dirty_state_hash:
        raise RuntimeError("repository dirty state changed after the audit was planned")


def _git(repository: Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("git")
    completed = subprocess.run(  # noqa: S603 - resolved git executable and vector arguments
        [executable, *args],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def _persist_audit_config(database: LabDatabase, audit: AuditRun) -> None:
    with database.transaction() as connection:
        connection.execute(
            "UPDATE security_audits SET state=?, config_json=?, updated_at=? WHERE id=?",
            (audit.state, audit.model_dump_json(), audit.updated_at.isoformat(), audit.id),
        )


def _write_and_store_reports(
    config: LabConfig, audit: AuditRun, findings: list[SecurityFinding]
) -> dict[str, Any]:
    paths = write_reports(config.artifacts_root / "security" / audit.id, audit, findings)
    artifacts = ArtifactStore(config.artifacts_root)
    hashes = {
        name: artifacts.put_file(Path(path), f"security-{audit.id}-{Path(path).name}").sha256
        for name, path in paths.items()
    }
    return {**paths, "artifact_sha256": hashes}


def source_catalog(config: LabConfig) -> dict[str, Any]:
    return build_catalog(config.project_root)
