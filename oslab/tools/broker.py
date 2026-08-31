from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from oslab.artifacts import ArtifactStore
from oslab.config import LabConfig
from oslab.database import LabDatabase
from oslab.debug import CrashEvidence, fingerprint_crash, normalize_log
from oslab.policy import PathPolicy, PolicyDenied, detect_evaluator_exploit
from oslab.process_runner import SafeProcessRunner
from oslab.schemas import ClassifiedError, ErrorKind, Outcome, ResultEnvelope, utc_now

REQUIRED_TOOLS = frozenset(
    {
        "repo.status",
        "repo.search",
        "repo.read",
        "repo.diff",
        "repo.create_worktree",
        "repo.apply_patch",
        "repo.reset_worktree",
        "repo.commit_local",
        "build.list_profiles",
        "build.run",
        "build.clean",
        "build.get_artifact",
        "build.read_diagnostics",
        "vm.create_run",
        "vm.boot",
        "vm.wait_for",
        "vm.console_send",
        "vm.save_snapshot",
        "vm.restore_snapshot",
        "vm.power_cycle",
        "vm.stop",
        "vm.status",
        "vm.collect",
        "test.list",
        "test.generate_fixture",
        "test.run",
        "test.replay",
        "test.run_regression",
        "test.compare",
        "debug.backtrace",
        "debug.registers",
        "debug.memory",
        "debug.disassemble",
        "debug.symbolize",
        "debug.classify_crash",
        "fuzz.list_targets",
        "fuzz.start",
        "fuzz.status",
        "fuzz.stop",
        "fuzz.replay",
        "fuzz.minimize",
        "fuzz.coverage",
        "report.record_hypothesis",
        "report.record_finding",
        "report.record_patch",
        "report.attach_artifact",
        "report.finalize_run",
        "policy.explain_denial",
        "policy.remaining_budget",
        "memory.search",
        "memory.get_experiment",
        "memory.find_similar_crashes",
        "memory.find_prior_hypotheses",
        "code.search",
        "code.symbol",
        "code.references",
        "git.history",
    }
)


class PatchChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    expected_sha256: str | None
    content: str


@dataclass
class ToolContext:
    config: LabConfig
    database: LabDatabase
    artifacts: ArtifactStore
    runner: SafeProcessRunner
    run_id: str
    remaining_tool_calls: int


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class CapabilityBroker:
    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self.policy = PathPolicy.create(
            context.config.allowed_roots, context.config.evaluator_roots
        )
        self.worktrees_root = (context.config.runtime_root / "worktrees").resolve()
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        self.denials: dict[str, dict[str, Any]] = {}
        self.handlers: dict[str, Handler] = {
            "repo.status": self._repo_status,
            "repo.search": self._repo_search,
            "repo.read": self._repo_read,
            "repo.diff": self._repo_diff,
            "repo.create_worktree": self._repo_create_worktree,
            "repo.apply_patch": self._repo_apply_patch,
            "repo.reset_worktree": self._repo_reset_worktree,
            "repo.commit_local": self._repo_commit_local,
            "debug.classify_crash": self._debug_classify,
            "policy.explain_denial": self._policy_explain,
            "policy.remaining_budget": self._policy_remaining,
            "report.record_hypothesis": self._record_entity,
            "report.record_finding": self._record_entity,
            "report.record_patch": self._record_entity,
            "report.attach_artifact": self._attach_artifact,
            "report.finalize_run": self._record_entity,
            "code.search": self._repo_search,
            "git.history": self._git_history,
        }

    def tool_names(self) -> list[str]:
        return sorted(REQUIRED_TOOLS)

    async def invoke(self, tool: str, arguments: dict[str, Any]) -> ResultEnvelope:
        started = utc_now()
        if self.context.remaining_tool_calls <= 0:
            return self._error(
                tool,
                started,
                Outcome.POLICY_DENIED,
                ErrorKind.POLICY,
                "tool-call budget exhausted",
            )
        self.context.remaining_tool_calls -= 1
        handler = self.handlers.get(tool)
        if handler is None:
            if tool not in REQUIRED_TOOLS:
                return self._error(
                    tool,
                    started,
                    Outcome.POLICY_DENIED,
                    ErrorKind.POLICY,
                    "tool is not in the capability registry",
                )
            return self._error(
                tool,
                started,
                Outcome.INFRA_ERROR,
                ErrorKind.UNSUPPORTED,
                "capability requires an initialized target backend",
            )
        try:
            data = await handler(arguments)
        except PolicyDenied as exc:
            denial_id = str(uuid4())
            self.denials[denial_id] = {"rule": exc.rule, "message": str(exc), "tool": tool}
            return self._error(
                tool,
                started,
                Outcome.POLICY_DENIED,
                ErrorKind.POLICY,
                str(exc),
                {"rule": exc.rule, "denial_id": denial_id},
            )
        except (FileNotFoundError, ValueError, OSError) as exc:
            return self._error(
                tool,
                started,
                Outcome.INFRA_ERROR,
                ErrorKind.VALIDATION if isinstance(exc, ValueError) else ErrorKind.PROCESS,
                str(exc),
            )
        ended = utc_now()
        return ResultEnvelope(
            status=Outcome.PASS,
            run_id=self.context.run_id,
            tool=tool,
            started_at=started,
            ended_at=ended,
            duration_ms=int((ended - started).total_seconds() * 1000),
            data=data,
        )

    def _error(
        self,
        tool: str,
        started: datetime,
        outcome: Outcome,
        kind: ErrorKind,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> ResultEnvelope:
        ended = utc_now()
        return ResultEnvelope(
            status=outcome,
            run_id=self.context.run_id,
            tool=tool,
            started_at=started,
            ended_at=ended,
            duration_ms=int((ended - started).total_seconds() * 1000),
            error=ClassifiedError(kind=kind, message=message, details=details or {}),
        )

    async def _git(self, args: list[str], cwd: Path, timeout: float = 30) -> dict[str, Any]:
        git = shutil.which("git")
        if not git:
            raise FileNotFoundError("git")
        result = await self.context.runner.run([git, *args], cwd=cwd, timeout=timeout)
        if result.returncode != 0:
            raise OSError(result.stderr or result.stdout)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "duration_ms": result.duration_ms,
        }

    def _authorized_root(self, value: Any, *, worktree_only: bool = False) -> Path:
        if not isinstance(value, str):
            raise ValueError("root must be a string")
        root = self.policy.authorize(Path(value), must_exist=True)
        if worktree_only and not PathPolicy._within(root, self.worktrees_root):
            raise PolicyDenied("worktree_required", "mutations require a disposable lab worktree")
        return root

    async def _repo_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments.get("root", str(self.context.config.project_root)))
        return await self._git(["status", "--porcelain=v2", "--branch"], root)

    async def _repo_diff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments["root"])
        result = await self._git(["diff", "--no-ext-diff", "--binary"], root)
        result["sha256"] = hashlib.sha256(result["stdout"].encode()).hexdigest()
        return result

    async def _repo_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments.get("root", str(self.context.config.project_root)))
        query = arguments.get("query")
        if not isinstance(query, str) or not query or len(query) > 500:
            raise ValueError("query must be a non-empty string of at most 500 characters")
        rg = shutil.which("rg")
        if not rg:
            raise FileNotFoundError("rg")
        result = await self.context.runner.run(
            [rg, "--json", "--no-messages", "--max-count", "100", "--", query, "."],
            cwd=root,
            timeout=30,
        )
        if result.returncode not in {0, 1}:
            raise OSError(result.stderr)
        return {
            "matches_jsonl": result.stdout,
            "content_hash": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "commit": (await self._git(["rev-parse", "HEAD"], root))["stdout"].strip(),
        }

    async def _repo_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments["root"])
        relative = arguments.get("path")
        if not isinstance(relative, str):
            raise ValueError("path must be a string")
        path = self.policy.authorize(root / relative, must_exist=True)
        if not PathPolicy._within(path, root):
            raise PolicyDenied("outside_repository", "read is outside the selected repository")
        data = path.read_bytes()
        limit = min(int(arguments.get("max_bytes", 262_144)), 1_048_576)
        preview = data[:limit].decode("utf-8", errors="replace")
        return {
            "path": str(path.relative_to(root)),
            "content": preview,
            "truncated": len(data) > limit,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }

    async def _repo_create_worktree(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = self._authorized_root(
            arguments.get("source", str(self.context.config.project_root))
        )
        commit = arguments.get("commit")
        if not isinstance(commit, str) or not commit:
            raise ValueError("commit is required")
        await self._git(["rev-parse", "--verify", f"{commit}^{{commit}}"], source)
        worktree_id = str(uuid4())
        target = self.worktrees_root / worktree_id
        result = await self._git(["worktree", "add", "--detach", str(target), commit], source, 60)
        actual = (await self._git(["rev-parse", "HEAD"], target))["stdout"].strip()
        return {**result, "worktree_id": worktree_id, "path": str(target), "commit": actual}

    async def _repo_apply_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments["worktree"], worktree_only=True)
        base_commit = arguments.get("base_commit")
        if not isinstance(base_commit, str) or not base_commit:
            raise ValueError("base_commit is required")
        actual_commit = (await self._git(["rev-parse", "HEAD"], root))["stdout"].strip()
        if actual_commit != base_commit:
            raise PolicyDenied(
                "base_commit_mismatch", "worktree HEAD does not match expected base commit"
            )
        raw_changes = arguments.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ValueError("changes must be a non-empty list")
        if len(raw_changes) > 20:
            raise PolicyDenied("patch_file_limit", "patch exceeds the 20-file limit")
        changes = [PatchChange.model_validate(item) for item in raw_changes]
        if sum(len(change.content.encode()) for change in changes) > 1_000_000:
            raise PolicyDenied("patch_size_limit", "patch exceeds the 1 MB content limit")
        snapshots: dict[Path, bytes | None] = {}
        before_hashes: dict[str, str | None] = {}
        after_hashes: dict[str, str] = {}
        try:
            for change in changes:
                if Path(change.path).is_absolute() or ".." in Path(change.path).parts:
                    raise PolicyDenied("path_traversal", "patch path must be repository-relative")
                target = self.policy.authorize(root / change.path, write=True)
                if not PathPolicy._within(target, root):
                    raise PolicyDenied("outside_worktree", "patch target escaped the worktree")
                existing = target.read_bytes() if target.exists() else None
                digest = hashlib.sha256(existing).hexdigest() if existing is not None else None
                if digest != change.expected_sha256:
                    raise PolicyDenied(
                        "expected_hash_mismatch", f"unexpected current content: {change.path}"
                    )
                snapshots[target] = existing
                before_hashes[change.path] = digest
            for change in changes:
                target = self.policy.authorize(root / change.path, write=True)
                data = change.content.encode("utf-8")
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temp_name = tempfile.mkstemp(prefix="oslab-patch-", dir=target.parent)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_name, target)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                expected_after = hashlib.sha256(data).hexdigest()
                if actual != expected_after:
                    raise OSError(f"post-write hash mismatch: {change.path}")
                after_hashes[change.path] = actual
            diff = (await self._git(["diff", "--no-ext-diff", "--binary"], root))["stdout"]
            exploits = detect_evaluator_exploit(diff)
            if exploits:
                raise PolicyDenied("evaluator_exploit", ", ".join(exploits))
            receipt = {
                "version": 1,
                "run_id": self.context.run_id,
                "base_commit": base_commit,
                "worktree_hash": hashlib.sha256(str(root).encode()).hexdigest(),
                "before": before_hashes,
                "after": after_hashes,
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "timestamp": utc_now().isoformat(),
            }
            record = self.context.artifacts.put_json(receipt, "patch-receipt.json")
            return {"diff": diff, "receipt": receipt, "receipt_sha256": record.sha256}
        except BaseException:
            for target, original in snapshots.items():
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original)
            raise

    async def _repo_reset_worktree(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments["worktree"], worktree_only=True)
        base_commit = arguments.get("base_commit")
        if not isinstance(base_commit, str):
            raise ValueError("base_commit is required")
        await self._git(["reset", "--hard", base_commit], root)
        await self._git(["clean", "-ffd"], root)
        return {"path": str(root), "commit": base_commit, "clean": True}

    async def _repo_commit_local(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments["worktree"], worktree_only=True)
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 200:
            raise ValueError("commit message must be 1-200 characters")
        await self._git(["add", "--all"], root)
        result = await self._git(
            [
                "-c",
                "user.name=Qwen OS Lab",
                "-c",
                "user.email=qwen-os-lab@local.invalid",
                "commit",
                "-m",
                message,
            ],
            root,
        )
        result["commit"] = (await self._git(["rev-parse", "HEAD"], root))["stdout"].strip()
        return result

    async def _git_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._authorized_root(arguments.get("root", str(self.context.config.project_root)))
        path = arguments.get("path")
        args = ["log", "--format=%H%x09%aI%x09%s", f"-n{min(int(arguments.get('limit', 20)), 100)}"]
        if isinstance(path, str):
            args.extend(["--", path])
        return await self._git(args, root)

    async def _debug_classify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        evidence = CrashEvidence(
            kind=str(arguments["kind"]),
            subsystem=str(arguments.get("subsystem", "unknown")),
            assertion=str(arguments["assertion"])
            if arguments.get("assertion") is not None
            else None,
            frames=tuple(str(value) for value in arguments.get("frames", [])),
            build_lineage=str(arguments["build_lineage"]),
            reproducer_hash=str(arguments["reproducer_hash"]),
        )
        return {
            "fingerprint": fingerprint_crash(evidence),
            "normalized_assertion": normalize_log(evidence.assertion or ""),
        }

    async def _policy_explain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        denial_id = arguments.get("denial_id")
        if not isinstance(denial_id, str) or denial_id not in self.denials:
            raise ValueError("unknown denial_id")
        return self.denials[denial_id]

    async def _policy_remaining(self, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        return {
            "tool_calls": self.context.remaining_tool_calls,
            "configured": self.context.config.budget.model_dump(),
        }

    async def _record_entity(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entity_type = str(arguments.get("entity_type", "report"))
        entity_id = str(arguments.get("id", uuid4()))
        payload = arguments.get("payload", {})
        self.context.database.record_entity(entity_id, self.context.run_id, entity_type, payload)
        record = self.context.artifacts.put_json(payload, f"{entity_type}-{entity_id}.json")
        return {"id": entity_id, "entity_type": entity_type, "artifact_sha256": record.sha256}

    async def _attach_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.policy.authorize(Path(str(arguments["path"])), must_exist=True)
        record = self.context.artifacts.put_file(
            path, str(arguments.get("logical_name", path.name))
        )
        return {
            "sha256": record.sha256,
            "size": record.size,
            "logical_name": record.logical_name,
        }
