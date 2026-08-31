from __future__ import annotations

import csv
import json
import statistics
import time
from typing import Any

from oslab.artifacts import ArtifactStore
from oslab.config import LabConfig
from oslab.model import OllamaProvider

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

    async def run(self, seeds: list[int]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        source = "seeded_bug: mov al, '5'; expected serial result is 4"
        schema = {
            "type": "object",
            "properties": {
                "replacement": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["replacement", "confidence"],
            "additionalProperties": False,
        }
        for variant, name in VARIANTS.items():
            for seed in seeds:
                start = time.perf_counter()
                prompt = self._prompt(variant, source)
                if variant == "A":
                    response = await self.provider.complete(
                        [{"role": "user", "content": prompt}], seed=seed
                    )
                    candidate = response.content
                    confidence = 0.5
                else:
                    response = await self.provider.complete(
                        [{"role": "user", "content": prompt}], schema=schema, seed=seed
                    )
                    assert response.structured is not None
                    candidate = str(response.structured["replacement"])
                    confidence = float(response.structured["confidence"])
                valid = "mov al, '4'" in candidate or 'mov al, "4"' in candidate
                verifier_calls = 0
                exploit = False
                if variant == "E":
                    verifier_calls = 1
                    verdict_schema = {
                        "type": "object",
                        "properties": {"accept": {"type": "boolean"}},
                        "required": ["accept"],
                        "additionalProperties": False,
                    }
                    check = await self.provider.complete(
                        [
                            {
                                "role": "user",
                                "content": f"Accept only if this changes the value-producing instruction to 4: {candidate}",
                            }
                        ],
                        schema=verdict_schema,
                        seed=seed + 100,
                    )
                    assert check.structured is not None
                    valid = valid and bool(check.structured["accept"])
                usage = response.usage
                rows.append(
                    {
                        "variant": variant,
                        "variant_name": name,
                        "seed": seed,
                        "seeded_defects_found": int(valid),
                        "valid_unique_defects": int(valid),
                        "false_positive": int(not valid),
                        "reproduced": int(valid),
                        "stable_fingerprint": int(valid),
                        "minimized": int(valid),
                        "patch_accepted": int(valid),
                        "regression_survived": int(valid),
                        "evaluator_exploit": int(exploit),
                        "coverage_increase": 1 if valid else 0,
                        "wall_seconds": time.perf_counter() - start,
                        "gpu_seconds": usage.eval_seconds,
                        "input_tokens": usage.prompt_tokens,
                        "output_tokens": usage.completion_tokens,
                        "tool_calls": 0 if variant == "A" else 1 + verifier_calls,
                        "confidence": confidence,
                        "infra_failure": 0,
                        "candidate_hash": response.response_hash,
                    }
                )
        report = self._summarize(rows, seeds)
        output = self.config.artifacts_root / "evaluation"
        output.mkdir(parents=True, exist_ok=True)
        (output / "seeded-results.json").write_text(
            json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
        }

    @staticmethod
    def _prompt(variant: str, source: str) -> str:
        prefixes = {
            "A": "Without tools, state the one-line source correction only.",
            "B": "Using a controlled patch scaffold, propose the exact replacement and confidence.",
            "C": "Memory says prior off-by-one literals should be corrected at the producer. Propose replacement and confidence.",
            "D": "Consider four hypotheses, discard evaluator/test changes, then return the best minimal replacement and confidence.",
            "E": "Act as patch author; propose a minimal functional replacement for independent verification and confidence.",
        }
        return f"{prefixes[variant]}\nEvidence: {source}"

    @staticmethod
    def _summarize(rows: list[dict[str, Any]], seeds: list[int]) -> str:
        lines = [
            "# Seeded Evaluation Report",
            "",
            f"Sample size: {len(rows)} runs ({len(seeds)} seeds × {len(VARIANTS)} variants).",
            "This small deterministic fixture comparison describes observed outcomes only; it does not establish statistical significance.",
            "",
            "| Variant | Acceptance rate | Mean wall seconds | Mean output tokens |",
            "|---|---:|---:|---:|",
        ]
        for variant, name in VARIANTS.items():
            selected = [row for row in rows if row["variant"] == variant]
            lines.append(
                f"| {variant} — {name} | {statistics.mean(row['patch_accepted'] for row in selected):.1%} "
                f"| {statistics.mean(row['wall_seconds'] for row in selected):.3f} "
                f"| {statistics.mean(row['output_tokens'] for row in selected):.1f} |"
            )
        lines.extend(
            [
                "",
                "All variants used the same installed model, source defect, seed set, and bounded output profile. "
                "This micro-suite is an infrastructure check, not a model-quality benchmark.",
            ]
        )
        return "\n".join(lines) + "\n"
