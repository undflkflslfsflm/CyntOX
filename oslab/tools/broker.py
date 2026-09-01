from __future__ import annotations

import hashlib
import json
import os
import re
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
from oslab.fuzz import replay_fixture_input, run_fixture_fuzz
from oslab.memory import MemoryIndex
from oslab.policy import PathPolicy, PolicyDenied, detect_evaluator_exploit
from oslab.process_runner import SafeProcessRunner
from oslab.qemu import DockerQemuBackend, FixtureResult
from oslab.schemas import ClassifiedError, ErrorKind, Outcome, ResultEnvelope, utc_now
from oslab.targets import inspect_targets, list_manifest_build_profiles, run_manifest_build

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


class BuildRunFailed(RuntimeError):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("build failed")
        self.result = result


class CapabilityBroker:
    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self.policy = PathPolicy.create(
            context.config.allowed_roots, context.config.evaluator_roots
        )
        self.worktrees_root = (context.config.runtime_root / "worktrees").resolve()
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        self.denials: dict[str, dict[str, Any]] = {}
        self.vm_sessions: dict[str, DockerQemuBackend] = {}
        self.vm_metadata: dict[str, dict[str, Any]] = {}
        self.handlers: dict[str, Handler] = {
            "repo.status": self._repo_status,
            "repo.search": self._repo_search,
            "repo.read": self._repo_read,
            "repo.diff": self._repo_diff,
            "repo.create_worktree": self._repo_create_worktree,
            "repo.apply_patch": self._repo_apply_patch,
            "repo.reset_worktree": self._repo_reset_worktree,
            "repo.commit_local": self._repo_commit_local,
            "build.list_profiles": self._build_list_profiles,
            "build.run": self._build_run,
            "build.clean": self._build_clean,
            "build.get_artifact": self._build_get_artifact,
            "build.read_diagnostics": self._build_read_diagnostics,
            "vm.create_run": self._vm_create_run,
            "vm.boot": self._vm_boot,
            "vm.wait_for": self._vm_wait_for,
            "vm.console_send": self._vm_console_send,
            "vm.save_snapshot": self._vm_save_snapshot,
            "vm.restore_snapshot": self._vm_restore_snapshot,
            "vm.power_cycle": self._vm_power_cycle,
            "vm.stop": self._vm_stop,
            "vm.status": self._vm_status,
            "vm.collect": self._vm_collect,
            "test.list": self._test_list,
            "test.generate_fixture": self._test_generate_fixture,
            "test.run": self._test_run,
            "test.replay": self._test_replay,
            "test.run_regression": self._test_run_regression,
            "test.compare": self._test_compare,
            "debug.backtrace": self._debug_backtrace,
            "debug.registers": self._debug_registers,
            "debug.memory": self._debug_memory,
            "debug.disassemble": self._debug_disassemble,
            "debug.symbolize": self._debug_symbolize,
            "debug.classify_crash": self._debug_classify,
            "fuzz.list_targets": self._fuzz_list_targets,
            "fuzz.start": self._fuzz_start,
            "fuzz.status": self._fuzz_status,
            "fuzz.stop": self._fuzz_stop,
            "fuzz.replay": self._fuzz_replay,
            "fuzz.minimize": self._fuzz_minimize,
            "fuzz.coverage": self._fuzz_coverage,
            "policy.explain_denial": self._policy_explain,
            "policy.remaining_budget": self._policy_remaining,
            "report.record_hypothesis": self._record_hypothesis,
            "report.record_finding": self._record_finding,
            "report.record_patch": self._record_patch,
            "report.attach_artifact": self._attach_artifact,
            "report.finalize_run": self._finalize_run,
            "memory.search": self._memory_search,
            "memory.get_experiment": self._memory_get_experiment,
            "memory.find_similar_crashes": self._memory_find_similar_crashes,
            "memory.find_prior_hypotheses": self._memory_find_prior_hypotheses,
            "code.search": self._repo_search,
            "code.symbol": self._code_symbol,
            "code.references": self._code_references,
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
        except BuildRunFailed as exc:
            return self._error(
                tool,
                started,
                Outcome.BUILD_ERROR,
                ErrorKind.BUILD,
                "build failed",
                exc.result,
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

    def _target_root(self, arguments: dict[str, Any]) -> Path:
        value = arguments.get(
            "root", arguments.get("worktree", str(self.context.config.project_root))
        )
        return self._authorized_root(value)

    def _fixture_target(self, arguments: dict[str, Any]) -> str:
        target = str(arguments.get("target", "fixture"))
        if target != "fixture":
            inspected = inspect_targets(self.context.config)
            raise ValueError(
                "only the fixture target is initialized; "
                f"real OS status: {inspected['real_os'].get('status')}"
            )
        return target

    def _manifest_target_root(self, arguments: dict[str, Any]) -> tuple[str, Path]:
        target = str(arguments.get("target", "fixture"))
        if target == "fixture":
            raise ValueError("manifest target helper requires a non-fixture target")
        raw_root = arguments.get("repo", arguments.get("root"))
        if not isinstance(raw_root, str) or not raw_root:
            raise ValueError("real target requires repo/root")
        return target, self._authorized_root(raw_root)

    @staticmethod
    def _target_matches_manifest(requested: str, actual: str) -> bool:
        return requested in {"real", actual}

    def _fixture_mode(self, arguments: dict[str, Any], default: str = "pass") -> str:
        mode = str(arguments.get("test_id", arguments.get("mode", default)))
        if mode not in {"pass", "fail", "crash", "hang", "snapshot", "seeded", "infra"}:
            raise ValueError("unknown fixture test mode")
        return mode

    def _vm_session(self, arguments: dict[str, Any]) -> tuple[str, DockerQemuBackend]:
        session_id = str(arguments.get("session_id", ""))
        backend = self.vm_sessions.get(session_id)
        if backend is None:
            raise ValueError("unknown VM session_id")
        return session_id, backend

    def _fixture_backend(self, arguments: dict[str, Any] | None = None) -> DockerQemuBackend:
        root = self._target_root(arguments or {})
        return DockerQemuBackend(root, self.context.artifacts)

    @staticmethod
    def _result_data(result: FixtureResult) -> dict[str, Any]:
        return {
            "run_id": result.run_id,
            "outcome": result.outcome,
            "started_at": result.started_at,
            "ended_at": result.ended_at,
            "serial_log": result.serial_log,
            "events": list(result.events),
            "artifacts": result.artifacts,
            "details": result.details,
        }

    @staticmethod
    def _bounded_timeout(arguments: dict[str, Any], key: str, default: float) -> float:
        timeout = float(arguments.get(key, default))
        if timeout <= 0 or timeout > 60:
            raise ValueError(f"{key} must be between 0 and 60 seconds")
        return timeout

    def _bounded_build_timeout(self, arguments: dict[str, Any], key: str, default: float) -> float:
        timeout = float(arguments.get(key, default))
        maximum = min(self.context.config.budget.wall_seconds, 3600.0)
        if timeout <= 0 or timeout > maximum:
            raise ValueError(f"{key} must be between 0 and {maximum:g} seconds")
        return timeout

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

    async def _build_list_profiles(self, arguments: dict[str, Any]) -> dict[str, Any]:
        target = str(arguments.get("target", "fixture"))
        if target != "fixture":
            requested, root = self._manifest_target_root(arguments)
            summary = await list_manifest_build_profiles(root)
            if not self._target_matches_manifest(requested, str(summary["target"])):
                raise ValueError(
                    f"target {requested!r} does not match manifest target {summary['target']!r}"
                )
            return {
                "targets": inspect_targets(self.context.config, root),
                "profiles": summary["profiles"],
                "target": summary["target"],
                "source_root": summary["source_root"],
                "base_commit": summary["base_commit"],
                "git": summary["git"],
            }
        targets = inspect_targets(self.context.config)
        return {
            "targets": targets,
            "profiles": [
                {
                    "target": "fixture",
                    "profile": "debug",
                    "build": "pinned Docker image with NASM",
                    "boot": "QEMU TCG, QMP loopback, -nic none",
                    "instrumentation": ["assertion events", "serial protocol"],
                },
                {
                    "target": "fixture",
                    "profile": "release",
                    "build": "same source and toolchain as debug; optimized flags not applicable",
                    "boot": "QEMU TCG, QMP loopback, -nic none",
                    "instrumentation": ["serial protocol"],
                },
            ],
        }

    async def _build_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        target = str(arguments.get("target", "fixture"))
        if target != "fixture":
            requested, root = self._manifest_target_root(arguments)
            profile = str(arguments.get("profile", "debug"))
            result = await run_manifest_build(
                root,
                profile,
                self.context.runner,
                self.context.artifacts,
                worktrees_root=self.worktrees_root,
                timeout=self._bounded_build_timeout(arguments, "timeout", 300),
            )
            if not self._target_matches_manifest(requested, str(result["target"])):
                raise ValueError(
                    f"target {requested!r} does not match manifest target {result['target']!r}"
                )
            if not result["ok"]:
                raise BuildRunFailed(result)
            return result
        profile = str(arguments.get("profile", "debug"))
        if profile not in {"debug", "release"}:
            raise ValueError("fixture build profile must be debug or release")
        backend = self._fixture_backend(arguments)
        return {"target": "fixture", "profile": profile, **await backend.build_fixture()}

    async def _build_clean(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        root = self._target_root(arguments)
        runtime = self.policy.authorize(root / ".oslab", write=True)
        fixture = self.policy.authorize(runtime / "fixture", write=True)
        if not PathPolicy._within(fixture, runtime):
            raise PolicyDenied("outside_runtime", "fixture clean escaped the runtime root")
        removed: list[str] = []
        if fixture.exists():
            shutil.rmtree(fixture)
            removed.append(str(fixture))
        return {"target": "fixture", "removed": removed, "clean": True}

    async def _build_get_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        name = str(arguments.get("name", "boot-sector.bin"))
        if name not in {"boot-sector.bin", "base.raw", "overlay.qcow2"}:
            raise ValueError("unknown fixture build artifact")
        root = self._target_root(arguments)
        path = self.policy.authorize(root / ".oslab" / "fixture" / name, must_exist=True)
        record = self.context.artifacts.put_file(path, f"fixture-{name}")
        return {
            "target": "fixture",
            "name": name,
            "path": str(path),
            "sha256": record.sha256,
            "size": record.size,
            "media_type": record.media_type,
        }

    async def _build_read_diagnostics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        diagnostics = await self._fixture_backend(arguments).ensure_toolchain()
        return {"target": "fixture", **diagnostics}

    async def _vm_create_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        seed = int(arguments.get("seed", 1))
        test_id = str(arguments.get("test_id", "interactive"))
        session_id = str(uuid4())
        self.vm_sessions[session_id] = self._fixture_backend(arguments)
        self.vm_metadata[session_id] = {
            "target": "fixture",
            "seed": seed,
            "test_id": test_id,
            "created_at": utc_now().isoformat(),
            "state": "created",
        }
        return {"session_id": session_id, **self.vm_metadata[session_id]}

    async def _vm_boot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        if arguments.get("session_id") in (None, ""):
            created = await self._vm_create_run(arguments)
            session_id = str(created["session_id"])
            backend = self.vm_sessions[session_id]
        else:
            session_id, backend = self._vm_session(arguments)
        seed = int(arguments.get("seed", self.vm_metadata[session_id]["seed"]))
        test_id = str(arguments.get("test_id", self.vm_metadata[session_id]["test_id"]))
        result = await backend.boot(seed=seed, test_id=test_id)
        self.vm_metadata[session_id].update({"state": "running", "boot": result})
        return {"session_id": session_id, **result}

    async def _vm_wait_for(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        pattern = str(arguments.get("pattern", '"event":"READY"'))
        if not pattern or len(pattern) > 200:
            raise ValueError("pattern must be 1..200 characters")
        text = await backend.wait_for(pattern, self._bounded_timeout(arguments, "timeout", 10))
        digest = hashlib.sha256(text.encode()).hexdigest()
        return {
            "session_id": session_id,
            "pattern": pattern,
            "serial_sha256": digest,
            "serial": text,
        }

    async def _vm_console_send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        command = str(arguments.get("command", ""))
        await backend.send(command)
        return {"session_id": session_id, "sent": command}

    async def _vm_save_snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        name = str(arguments.get("name", "snapshot"))
        result = await backend.save_snapshot(name)
        return {"session_id": session_id, "name": name, "qmp": result}

    async def _vm_restore_snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        name = str(arguments.get("name", "snapshot"))
        result = await backend.restore_snapshot(name)
        return {"session_id": session_id, "name": name, "qmp": result}

    async def _vm_power_cycle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        await backend.power_cycle()
        self.vm_metadata[session_id]["state"] = "running"
        return {"session_id": session_id, "powered": "cycle"}

    async def _vm_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        await backend.stop()
        self.vm_sessions.pop(session_id, None)
        metadata = self.vm_metadata.pop(session_id, {})
        metadata["state"] = "stopped"
        return {"session_id": session_id, **metadata}

    async def _vm_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        running = backend.process is not None and backend.process.returncode is None
        return {
            "session_id": session_id,
            "running": running,
            "returncode": None if backend.process is None else backend.process.returncode,
            "serial_bytes": len(backend.serial),
            "stderr_bytes": len(backend.stderr),
            "metadata": self.vm_metadata.get(session_id, {}),
        }

    async def _vm_collect(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, backend = self._vm_session(arguments)
        serial_text = backend.serial.decode("utf-8", errors="replace")
        stderr_text = backend.stderr.decode("utf-8", errors="replace")
        serial = self.context.artifacts.put_bytes(
            serial_text.encode(), f"vm-{session_id}-serial.log", "text/plain"
        )
        stderr = self.context.artifacts.put_bytes(
            stderr_text.encode(), f"vm-{session_id}-stderr.log", "text/plain"
        )
        return {
            "session_id": session_id,
            "artifacts": {"serial": serial.sha256, "stderr": stderr.sha256},
            "serial_tail": serial_text[-4000:],
            "stderr_tail": stderr_text[-4000:],
        }

    async def _test_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        return {
            "target": "fixture",
            "tests": [
                {"id": "pass", "expected": Outcome.PASS, "command": "P"},
                {"id": "fail", "expected": Outcome.FAIL, "command": "F"},
                {"id": "crash", "expected": Outcome.CRASH, "command": "C"},
                {"id": "hang", "expected": Outcome.HANG, "command": "H"},
                {"id": "snapshot", "expected": Outcome.PASS, "command": "I/I restore"},
                {"id": "seeded", "expected": Outcome.FAIL, "command": "B"},
            ],
        }

    async def _test_generate_fixture(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        mode = self._fixture_mode(arguments, "crash")
        seed = int(arguments.get("seed", 1))
        payload = {
            "target": "fixture",
            "mode": mode,
            "seed": seed,
            "sequence": {
                "pass": ["P"],
                "fail": ["F"],
                "crash": ["C"],
                "hang": ["H"],
                "snapshot": ["I", "save_snapshot:proof", "I", "restore_snapshot:proof", "I"],
                "seeded": ["B"],
                "infra": ["invalid-fixture-mode"],
            }[mode],
            "expected": {
                "pass": Outcome.PASS,
                "fail": Outcome.FAIL,
                "crash": Outcome.CRASH,
                "hang": Outcome.HANG,
                "snapshot": Outcome.PASS,
                "seeded": Outcome.FAIL,
                "infra": Outcome.INFRA_ERROR,
            }[mode],
        }
        record = self.context.artifacts.put_json(payload, f"fixture-test-{mode}.json")
        return {**payload, "artifact_sha256": record.sha256}

    async def _test_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        mode = self._fixture_mode(arguments)
        seed = int(arguments.get("seed", 1))
        result = await self._fixture_backend(arguments).exercise(mode, seed=seed)
        return self._result_data(result)

    async def _test_replay(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        if "input_path" in arguments:
            input_path = self.policy.authorize(Path(str(arguments["input_path"])), must_exist=True)
            mode = self._fixture_mode(arguments, "crash")
            return await replay_fixture_input(self.context.config, mode, input_path)
        return await self._test_run(arguments)

    async def _test_run_regression(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        seed = int(arguments.get("seed", 1))
        backend = self._fixture_backend(arguments)
        pass_result = await backend.exercise("pass", seed=seed)
        seeded_result = await backend.exercise("seeded", seed=seed)
        regression_passed = pass_result.outcome == Outcome.PASS and seeded_result.outcome in {
            Outcome.PASS,
            Outcome.FAIL,
        }
        return {
            "target": "fixture",
            "passed": regression_passed,
            "checks": {
                "pass": self._result_data(pass_result),
                "seeded_exercised": self._result_data(seeded_result),
            },
        }

    async def _test_compare(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "left_artifact" in arguments and "right_artifact" in arguments:
            left_bytes = self.context.artifacts.get(str(arguments["left_artifact"]))
            right_bytes = self.context.artifacts.get(str(arguments["right_artifact"]))
            return {
                "kind": "artifact-bytes",
                "equal": left_bytes == right_bytes,
                "left_sha256": hashlib.sha256(left_bytes).hexdigest(),
                "right_sha256": hashlib.sha256(right_bytes).hexdigest(),
            }
        left_json = json.dumps(arguments.get("left"), sort_keys=True, default=str)
        right_json = json.dumps(arguments.get("right"), sort_keys=True, default=str)
        return {
            "kind": "json-value",
            "equal": left_json == right_json,
            "left_sha256": hashlib.sha256(left_json.encode()).hexdigest(),
            "right_sha256": hashlib.sha256(right_json.encode()).hexdigest(),
        }

    async def _debug_backtrace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        return {
            "available": False,
            "target": "fixture",
            "reason": (
                "the included proof target is a 16-bit boot-sector fixture with serial "
                "evidence and QMP, but no kernel stack unwinder or debug symbol table"
            ),
            "recommended_tool": "debug.classify_crash",
        }

    async def _debug_registers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("session_id") in (None, ""):
            return {
                "available": False,
                "reason": "register capture requires a running vm.create_run/vm.boot session",
            }
        session_id, backend = self._vm_session(arguments)
        if backend.qmp is None:
            raise ValueError("VM QMP is not connected")
        registers = await backend.qmp.hmp(
            "info registers", self._bounded_timeout(arguments, "timeout", 10)
        )
        return {"session_id": session_id, "registers": registers}

    async def _debug_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("session_id") in (None, ""):
            return {
                "available": False,
                "reason": "memory capture requires a running vm.create_run/vm.boot session",
            }
        session_id, backend = self._vm_session(arguments)
        address = str(arguments.get("address", "0x7c00")).lower()
        count = min(max(int(arguments.get("count", 16)), 1), 256)
        if not re.fullmatch(r"0x[0-9a-f]{1,16}", address):
            raise ValueError("address must be a bounded hexadecimal physical address")
        if backend.qmp is None:
            raise ValueError("VM QMP is not connected")
        memory = await backend.qmp.hmp(
            f"xp /{count}xb {address}", self._bounded_timeout(arguments, "timeout", 10)
        )
        return {"session_id": session_id, "address": address, "count": count, "memory": memory}

    async def _debug_disassemble(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        root = self._target_root(arguments)
        binary = root / ".oslab" / "fixture" / "boot-sector.bin"
        if not binary.is_file():
            await self._fixture_backend(arguments).build_fixture()
        code, stdout, stderr = await self._fixture_backend(arguments)._command(
            [
                self._fixture_backend(arguments).docker,
                "run",
                "--rm",
                "--mount",
                f"type=bind,source={root},target=/lab",
                "qwen-os-lab-qemu:bookworm",
                "ndisasm",
                "-b",
                "16",
                "/lab/.oslab/fixture/boot-sector.bin",
            ],
            30,
        )
        if code != 0:
            raise OSError(stderr or stdout)
        return {
            "target": "fixture",
            "binary": str(binary),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "disassembly": stdout,
        }

    async def _debug_symbolize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        root = self._target_root(arguments)
        source = root / "fixtures" / "boot" / "boot.asm"
        offset = str(arguments.get("offset", "unknown"))
        return {
            "target": "fixture",
            "offset": offset,
            "available": source.is_file(),
            "source": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()
            if source.is_file()
            else None,
            "note": "boot-sector fixture symbols are source labels, not linked DWARF symbols",
        }

    async def _fuzz_list_targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        return {
            "targets": [
                {
                    "target": "fixture",
                    "fuzzer": "seeded corpus mutation over serial protocol modes",
                    "modes": ["pass", "fail", "crash", "hang", "seeded", "induced-infra"],
                    "network": "none",
                }
            ]
        }

    async def _fuzz_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._fixture_target(arguments)
        campaign_id = str(arguments.get("campaign_id", f"broker-{self.context.run_id}"))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", campaign_id):
            raise ValueError("campaign_id must be a short filesystem-safe identifier")
        iterations = int(arguments.get("iterations", 6))
        seed = int(arguments.get("seed", 101))
        return await run_fixture_fuzz(
            self.context.config, campaign_id, seed=seed, total_iterations=iterations
        )

    async def _fuzz_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(arguments.get("campaign_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", campaign_id):
            raise ValueError("campaign_id is required")
        root = self.context.config.runtime_root / "fuzz" / campaign_id
        checkpoint = root / "checkpoint.json"
        observations = root / "observations.json"
        return {
            "campaign_id": campaign_id,
            "checkpoint_exists": checkpoint.is_file(),
            "checkpoint": json.loads(checkpoint.read_text(encoding="utf-8"))
            if checkpoint.is_file()
            else None,
            "observation_count": len(json.loads(observations.read_text(encoding="utf-8")))
            if observations.is_file()
            else 0,
        }

    async def _fuzz_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        status = await self._fuzz_status(arguments)
        return {
            **status,
            "stopped": True,
            "note": "fixture fuzzing runs are bounded foreground jobs",
        }

    async def _fuzz_replay(self, arguments: dict[str, Any]) -> dict[str, Any]:
        input_path = self.policy.authorize(Path(str(arguments["input_path"])), must_exist=True)
        mode = str(arguments.get("mode", "crash"))
        return await replay_fixture_input(self.context.config, mode, input_path)

    async def _fuzz_minimize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(arguments.get("campaign_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", campaign_id):
            raise ValueError("campaign_id is required")
        root = self.context.config.runtime_root / "fuzz" / campaign_id
        checkpoint = root / "checkpoint.json"
        if not checkpoint.is_file():
            raise FileNotFoundError(checkpoint)
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        crash = next(
            (
                finding
                for finding in dict(payload.get("unique_findings", {})).values()
                if finding.get("mode") == "crash"
            ),
            None,
        )
        if not isinstance(crash, dict):
            raise ValueError("campaign has no crash finding to minimize")
        corpus_dir = Path(str(payload["corpus_dir"]))
        original = self.policy.authorize(
            corpus_dir / f"{crash['input_sha256']}.bin", must_exist=True
        ).read_bytes()
        minimized = original[:1]
        digest = hashlib.sha256(minimized).hexdigest()
        minimized_path = self.policy.authorize(
            corpus_dir / f"broker-min-{digest[:16]}.bin", write=True
        )
        minimized_path.write_bytes(minimized)
        replay = await replay_fixture_input(self.context.config, "crash", minimized_path)
        return {
            "campaign_id": campaign_id,
            "original_bytes": len(original),
            "minimized_bytes": len(minimized),
            "input_sha256": digest,
            "path": str(minimized_path),
            "replay": replay,
        }

    async def _fuzz_coverage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        status = await self._fuzz_status(arguments)
        checkpoint = status.get("checkpoint") or {}
        findings = dict(checkpoint.get("unique_findings", {}))
        observations_path = (
            self.context.config.runtime_root
            / "fuzz"
            / str(arguments.get("campaign_id", ""))
            / "observations.json"
        )
        observations = (
            json.loads(observations_path.read_text(encoding="utf-8"))
            if observations_path.is_file()
            else []
        )
        modes = sorted(
            {str(row.get("mode")) for row in observations}
            | {str(value.get("mode")) for value in findings.values()}
        )
        outcomes = sorted(
            {str(row.get("outcome")) for row in observations}
            | {str(value.get("outcome")) for value in findings.values()}
        )
        return {
            "kind": "fixture protocol-state coverage (not compiler instrumentation)",
            "modes": modes,
            "outcomes": outcomes,
            "complete": set(modes) >= {"pass", "fail", "crash", "hang", "seeded", "induced-infra"},
            "unique_findings": len(findings),
        }

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

    async def _record_hypothesis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._record_entity({**arguments, "entity_type": "hypothesis"})

    async def _record_finding(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._record_entity({**arguments, "entity_type": "finding"})

    async def _record_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._record_entity({**arguments, "entity_type": "patch"})

    async def _finalize_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._record_entity({**arguments, "entity_type": "run-summary"})

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

    async def _memory_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", ""))
        limit = min(max(int(arguments.get("limit", 10)), 1), 50)
        hits = MemoryIndex(self.context.database).search(query, limit)
        return {
            "query": query,
            "hits": [
                {
                    "source": hit.source,
                    "content": hit.content,
                    "commit_id": hit.commit_id,
                    "content_hash": hit.content_hash,
                    "score": hit.score,
                }
                for hit in hits
            ],
        }

    async def _memory_get_experiment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        experiment_id = str(arguments.get("experiment_id", arguments.get("id", "")))
        if not experiment_id:
            raise ValueError("experiment_id is required")
        with self.context.database.connect() as connection:
            experiment = connection.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            runs = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM runs WHERE experiment_id = ? ORDER BY created_at",
                    (experiment_id,),
                ).fetchall()
            ]
            entities = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM entities WHERE id = ? OR run_id = ? ORDER BY created_at",
                    (experiment_id, experiment_id),
                ).fetchall()
            ]
        return {
            "experiment": dict(experiment) if experiment is not None else None,
            "runs": runs,
            "entities": entities,
        }

    async def _memory_find_similar_crashes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("fingerprint", arguments.get("query", "")))
        limit = min(max(int(arguments.get("limit", 10)), 1), 50)
        like = f"%{query}%"
        with self.context.database.connect() as connection:
            findings = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM findings WHERE fingerprint LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (like, limit),
                ).fetchall()
            ]
            entity_hits = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM entities WHERE entity_type IN ('finding', 'crash') "
                    "AND payload_json LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (like, limit),
                ).fetchall()
            ]
        return {"query": query, "findings": findings, "entities": entity_hits}

    async def _memory_find_prior_hypotheses(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", ""))
        limit = min(max(int(arguments.get("limit", 10)), 1), 50)
        with self.context.database.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM entities WHERE entity_type = 'hypothesis' "
                    "AND payload_json LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{query}%", limit),
                ).fetchall()
            ]
        return {"query": query, "hypotheses": rows}

    async def _code_symbol(self, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = str(arguments.get("symbol", arguments.get("query", "")))
        if not symbol or len(symbol) > 200:
            raise ValueError("symbol must be 1..200 characters")
        return await self._repo_search({**arguments, "query": rf"\b{re.escape(symbol)}\b"})

    async def _code_references(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("symbol", arguments.get("query", "")))
        if not query or len(query) > 200:
            raise ValueError("symbol/query must be 1..200 characters")
        return await self._repo_search({**arguments, "query": rf"\b{re.escape(query)}\b"})
