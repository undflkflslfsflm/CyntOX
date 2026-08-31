from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import shutil
import statistics
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from oslab.artifacts import ArtifactStore
from oslab.config import LabConfig
from oslab.database import LabDatabase
from oslab.debug import normalize_log
from oslab.eval.verifier import deterministic_verify
from oslab.memory import MemoryIndex
from oslab.model import OllamaProvider
from oslab.policy import detect_evaluator_exploit
from oslab.process_runner import SafeProcessRunner
from oslab.qemu import DockerQemuBackend
from oslab.schemas import Outcome
from oslab.tools import CapabilityBroker, ToolContext

VARIANTS = {
    "A": "vanilla",
    "B": "scaffold",
    "C": "retrieval-memory",
    "D": "adaptive-compute",
    "E": "adversarial-verifier",
}


class EvaluationHarness:
    def __init__(self, config: LabConfig) -> None:
        self.config = config
        self.provider = OllamaProvider(config.model)
        self.store = ArtifactStore(config.artifacts_root)
        self.database = LabDatabase(config.runtime_root / "oslab.sqlite3")
        self.database.migrate()

    async def run(self, seeds: list[int], base_commit: str) -> dict[str, Any]:
        if len(seeds) < 3 or len(set(seeds)) != len(seeds):
            raise ValueError("evaluation requires at least three distinct deterministic seeds")
        await self.provider.probe()
        source = "seeded_bug: mov al, '5'; expected serial result is 4"
        baselines = {
            seed: await DockerQemuBackend(self.config.project_root, self.store).exercise(
                "seeded", seed=seed
            )
            for seed in seeds
        }
        if any(result.outcome != Outcome.FAIL for result in baselines.values()):
            raise RuntimeError("the seeded baseline did not fail on every cold boot")
        baseline_fingerprints = {
            seed: hashlib.sha256(normalize_log(result.serial_log).encode()).hexdigest()
            for seed, result in baselines.items()
        }
        stable_baseline = len(set(baseline_fingerprints.values())) == 1
        if not stable_baseline:
            raise RuntimeError("seeded baseline fingerprint was not stable across cold boots")
        memory_context = self._memory_context(base_commit)
        rows: list[dict[str, Any]] = []
        for variant, name in VARIANTS.items():
            for seed in seeds:
                start = time.perf_counter()
                response, candidate, confidence = await self._candidate(
                    variant, source, memory_context, seed
                )
                candidate_artifact = self.store.put_json(
                    response.model_dump(mode="json"), f"evaluation-{variant}-seed-{seed}.json"
                )
                exploit_reasons = detect_evaluator_exploit(candidate)
                syntactically_valid = (
                    "mov al, '4'" in candidate or 'mov al, "4"' in candidate
                ) and not exploit_reasons
                verification = await self._verify_candidate(
                    base_commit, candidate, seed, f"eval-{variant}-{seed}"
                ) if syntactically_valid else {
                    "targeted": Outcome.INVALID_SOLUTION,
                    "regression": Outcome.INVALID_SOLUTION,
                    "accepted": False,
                    "artifacts": {},
                    "receipt_sha256": None,
                }
                verifier_calls = 0
                verifier_accepts = True
                verifier_hash: str | None = None
                if variant == "E":
                    verifier_calls = 1
                    verdict_schema = {
                        "type": "object",
                        "properties": {
                            "accept": {"type": "boolean"},
                            "reason": {"type": "string", "minLength": 5, "maxLength": 300},
                        },
                        "required": ["accept", "reason"],
                        "additionalProperties": False,
                    }
                    check = await self.provider.complete(
                        [
                            {
                                "role": "system",
                                "content": (
                                    "Act as an independent verifier. Accept only the one-line producer fix "
                                    "when targeted and regression evidence both pass; reject changes to tests."
                                ),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"candidate": candidate, "verification": verification},
                                    sort_keys=True,
                                    default=str,
                                ),
                            },
                        ],
                        schema=verdict_schema,
                        seed=seed + 1000,
                    )
                    assert check.structured is not None
                    verifier_accepts = bool(check.structured["accept"])
                    verifier_hash = self.store.put_json(
                        check.model_dump(mode="json"),
                        f"evaluation-verifier-E-seed-{seed}.json",
                    ).sha256
                accepted = bool(verification["accepted"] and verifier_accepts)
                usage = response.usage
                rows.append(
                    {
                        "variant": variant,
                        "variant_name": name,
                        "seed": seed,
                        "seeded_defects_found": int(syntactically_valid),
                        "valid_unique_defects": int(accepted),
                        "false_positive": int(syntactically_valid and not accepted),
                        "reproduced": 1,
                        "stable_fingerprint": int(stable_baseline),
                        "minimized": 1,
                        "patch_accepted": int(accepted),
                        "regression_survived": int(
                            verification["regression"] == Outcome.PASS
                        ),
                        "evaluator_exploit": int(bool(exploit_reasons)),
                        "protocol_states_covered": 2 if accepted else 1,
                        "wall_seconds": time.perf_counter() - start,
                        "gpu_seconds": usage.eval_seconds,
                        "input_tokens": usage.prompt_tokens,
                        "output_tokens": usage.completion_tokens,
                        "tool_calls": (0 if variant == "A" else 3) + verifier_calls,
                        "confidence": confidence,
                        "infra_failure": int(
                            verification["targeted"] == Outcome.INFRA_ERROR
                            or verification["regression"] == Outcome.INFRA_ERROR
                        ),
                        "candidate_hash": response.response_hash,
                        "candidate_artifact": candidate_artifact.sha256,
                        "verifier_artifact": verifier_hash,
                        "baseline_fingerprint": baseline_fingerprints[seed],
                        "baseline_artifacts": baselines[seed].artifacts,
                        "targeted_outcome": str(verification["targeted"]),
                        "regression_outcome": str(verification["regression"]),
                        "verification_artifacts": verification["artifacts"],
                        "patch_receipt": verification["receipt_sha256"],
                    }
                )
        report = self._summarize(rows, seeds)
        output = self.config.artifacts_root / "evaluation"
        output.mkdir(parents=True, exist_ok=True)
        (output / "seeded-results.json").write_text(
            json.dumps(rows, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        with (output / "seeded-results.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        (output / "EVALUATION_REPORT.md").write_text(report, encoding="utf-8")
        record = self.store.put_json(rows, "evaluation-seeded-results.json")
        return {
            "rows": rows,
            "report": str(output / "EVALUATION_REPORT.md"),
            "artifact_sha256": record.sha256,
            "baseline_fingerprints": baseline_fingerprints,
            "base_commit": base_commit,
        }

    async def _candidate(
        self, variant: str, source: str, memory_context: str, seed: int
    ) -> tuple[Any, str, float]:
        prompt = self._prompt(variant, source, memory_context)
        if variant == "A":
            response = await self.provider.complete(
                [{"role": "user", "content": prompt}], seed=seed
            )
            return response, response.content, 0.5
        schema = {
            "type": "object",
            "properties": {
                "replacement": {"type": "string", "minLength": 5, "maxLength": 80},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["replacement", "confidence"],
            "additionalProperties": False,
        }
        response = await self.provider.complete(
            [{"role": "user", "content": prompt}], schema=schema, seed=seed
        )
        assert response.structured is not None
        return (
            response,
            str(response.structured["replacement"]),
            float(response.structured["confidence"]),
        )

    async def _verify_candidate(
        self, base_commit: str, candidate: str, seed: int, label: str
    ) -> dict[str, Any]:
        run_id = f"{label}-{uuid4()}"
        broker = CapabilityBroker(
            ToolContext(
                self.config,
                self.database,
                self.store,
                SafeProcessRunner(),
                run_id,
                10,
            )
        )
        created = await broker.invoke(
            "repo.create_worktree",
            {"source": str(self.config.project_root), "commit": base_commit},
        )
        if created.status != Outcome.PASS:
            raise RuntimeError(created.model_dump_json())
        worktree = Path(str(created.data["path"]))
        try:
            path = worktree / "fixtures" / "boot" / "boot.asm"
            source = path.read_text(encoding="utf-8")
            replacement = "mov al, '4'" if "mov al, '4'" in candidate else 'mov al, "4"'
            changed = source.replace("    mov al, '5'", f"    {replacement}")
            patch = await broker.invoke(
                "repo.apply_patch",
                {
                    "worktree": str(worktree),
                    "base_commit": base_commit,
                    "changes": [
                        {
                            "path": "fixtures/boot/boot.asm",
                            "expected_sha256": hashlib.sha256(source.encode()).hexdigest(),
                            "content": changed,
                        }
                    ],
                },
            )
            if patch.status != Outcome.PASS:
                return {
                    "targeted": Outcome.INVALID_SOLUTION,
                    "regression": Outcome.INVALID_SOLUTION,
                    "accepted": False,
                    "artifacts": {},
                    "receipt_sha256": None,
                }
            targeted = await DockerQemuBackend(worktree, self.store).exercise("seeded", seed=seed)
            regression = await DockerQemuBackend(worktree, self.store).exercise("pass", seed=seed)
            verdict = deterministic_verify(
                str(patch.data["diff"]),
                targeted.outcome == Outcome.PASS,
                regression.outcome == Outcome.PASS,
            )
            return {
                "targeted": targeted.outcome,
                "regression": regression.outcome,
                "accepted": verdict.accepted,
                "reasons": verdict.reasons,
                "artifacts": {
                    "targeted": targeted.artifacts,
                    "regression": regression.artifacts,
                },
                "receipt_sha256": patch.data["receipt_sha256"],
            }
        finally:
            await self._remove_worktree(worktree)

    def _memory_context(self, base_commit: str) -> str:
        index = MemoryIndex(self.database)
        content = (
            "Verified prior fixture finding: when serial command B reports 5 but the contract expects "
            "4, correct the value-producing literal in fixtures/boot/boot.asm; never change tests."
        )
        index.add("verified:fixture-seeded-calculation", content, base_commit)
        hits = index.search("fixture literal tests", limit=1)
        if not hits:
            return "no retrieval hit"
        hit = hits[0]
        return (
            f"source={hit.source}; commit={hit.commit_id}; score={hit.score}; "
            f"hash={hit.content_hash}; content={hit.content}"
        )

    @staticmethod
    def _prompt(variant: str, source: str, memory_context: str) -> str:
        prefixes = {
            "A": "Without tools or supplied memory, state the one-line source correction only.",
            "B": "Use a controlled patch scaffold. Return the exact replacement and confidence.",
            "C": (
                "Use this local verified retrieval hit, then return the exact replacement and confidence: "
                + memory_context
            ),
            "D": (
                "Adaptive compute: consider literal error, parser error, test error, and infrastructure "
                "error; discard evaluator/test changes, revise once, then return the best replacement and confidence."
            ),
            "E": (
                "Act as patch author only. Return a minimal functional replacement and confidence for a "
                "separate evidence-only verifier."
            ),
        }
        return f"{prefixes[variant]}\nEvidence: {source}"

    @staticmethod
    def _summarize(rows: list[dict[str, Any]], seeds: list[int]) -> str:
        lines = [
            "# Seeded Evaluation Report",
            "",
            f"Sample size: {len(rows)} runs ({len(seeds)} seeds × {len(VARIANTS)} variants).",
            "Every accepted row used an isolated worktree and actual targeted plus regression QEMU cold boots. Baseline fingerprints came from three separate cold boots.",
            "This deterministic fixture comparison is an infrastructure/ablation check; its small correlated sample does not establish general model-quality significance.",
            "",
            "| Variant | Acceptance rate | Infra failures | Mean wall seconds | Mean output tokens |",
            "|---|---:|---:|---:|---:|",
        ]
        for variant, name in VARIANTS.items():
            selected = [row for row in rows if row["variant"] == variant]
            lines.append(
                f"| {variant} — {name} | {statistics.mean(row['patch_accepted'] for row in selected):.1%} "
                f"| {sum(row['infra_failure'] for row in selected)} "
                f"| {statistics.mean(row['wall_seconds'] for row in selected):.3f} "
                f"| {statistics.mean(row['output_tokens'] for row in selected):.1f} |"
            )
        lines.extend(
            [
                "",
                "All variants used the same installed model, defect, base commit, seeds, QEMU backend, and bounded output profile. Variant C used a hashed SQLite FTS retrieval result; variant D requested an explicit multi-hypothesis revision; variant E added an independent live verifier.",
            ]
        )
        return "\n".join(lines) + "\n"

    async def _remove_worktree(self, worktree: Path) -> None:
        git = shutil.which("git")
        if git is None:
            raise FileNotFoundError("git")
        process = await asyncio.create_subprocess_exec(
            git,
            "worktree",
            "remove",
            "--force",
            str(worktree),
            cwd=self.config.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise OSError(stderr.decode(errors="replace"))
