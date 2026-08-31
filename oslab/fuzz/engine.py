from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def mutate(data: bytes, seed: int) -> bytes:
    rng = random.Random(seed)  # noqa: S311 - reproducibility, not cryptography
    value = bytearray(data or b"\x00")
    operation = rng.randrange(4)
    position = rng.randrange(len(value))
    if operation == 0:
        value[position] ^= 1 << rng.randrange(8)
    elif operation == 1:
        value[position] = rng.randrange(256)
    elif operation == 2 and len(value) < 65536:
        value.insert(position, rng.randrange(256))
    elif len(value) > 1:
        del value[position]
    return bytes(value)


@dataclass
class FuzzCampaign:
    campaign_id: str
    seed: int
    corpus_dir: Path
    iterations: int = 0
    unique_inputs: set[str] = field(default_factory=set)
    unique_findings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def next_input(self, corpus: list[bytes]) -> bytes:
        if not corpus:
            corpus = [b"\x00"]
        chosen = corpus[self.iterations % len(corpus)]
        generated = mutate(chosen, self.seed + self.iterations)
        self.iterations += 1
        self.unique_inputs.add(hashlib.sha256(generated).hexdigest())
        return generated

    def record_finding(self, fingerprint: str, value: dict[str, Any]) -> bool:
        if fingerprint in self.unique_findings:
            return False
        self.unique_findings[fingerprint] = value
        return True

    def checkpoint(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "campaign_id": self.campaign_id,
            "seed": self.seed,
            "corpus_dir": str(self.corpus_dir),
            "iterations": self.iterations,
            "unique_inputs": sorted(self.unique_inputs),
            "unique_findings": self.unique_findings,
        }
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(path)

    @classmethod
    def resume(cls, path: Path) -> FuzzCampaign:
        payload = json.loads(path.read_text(encoding="utf-8"))
        campaign = cls(payload["campaign_id"], int(payload["seed"]), Path(payload["corpus_dir"]))
        campaign.iterations = int(payload["iterations"])
        campaign.unique_inputs = set(payload["unique_inputs"])
        campaign.unique_findings = dict(payload["unique_findings"])
        return campaign
