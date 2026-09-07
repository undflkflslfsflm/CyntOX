from __future__ import annotations

import hashlib
import os
from pathlib import Path

PROMPT_V2_CANDIDATE_ENV = "CYNTOX_MYTHOS_V2_CANDIDATE"

ADOPTED_PROMPT_VERSION = "v1"
ADOPTED_PROMPT_STATUS = "active"
ADOPTED_PROMPT_SENTINEL = "CYNTOX_MYTHOS_SYSTEM_PROMPT_V1"

CANDIDATE_PROMPT_VERSION = "v2"
CANDIDATE_PROMPT_STATUS = "candidate"
CANDIDATE_PROMPT_SENTINEL = "CYNTOX_MYTHOS_SYSTEM_PROMPT_V2"

# Freeze prompt selection for the lifetime of a process. This keeps launcher,
# proxy, manifests, and requests bound to one internally consistent prompt.
PROMPT_V2_CANDIDATE_ENABLED = os.environ.get(PROMPT_V2_CANDIDATE_ENV) == "1"
PROMPT_VERSION = CANDIDATE_PROMPT_VERSION if PROMPT_V2_CANDIDATE_ENABLED else ADOPTED_PROMPT_VERSION
PROMPT_STATUS = CANDIDATE_PROMPT_STATUS if PROMPT_V2_CANDIDATE_ENABLED else ADOPTED_PROMPT_STATUS
PROMPT_SENTINEL = (
    CANDIDATE_PROMPT_SENTINEL if PROMPT_V2_CANDIDATE_ENABLED else ADOPTED_PROMPT_SENTINEL
)


def adopted_prompt_path(root: Path | None = None) -> Path:
    repository_root = root if root is not None else Path(__file__).resolve().parents[1]
    return repository_root / "prompts" / "archive" / "mythos-system-v1.md"


def candidate_prompt_path(root: Path | None = None) -> Path:
    repository_root = root if root is not None else Path(__file__).resolve().parents[1]
    return repository_root / "prompts" / "mythos-system.md"


def canonical_prompt_path(root: Path | None = None) -> Path:
    if PROMPT_V2_CANDIDATE_ENABLED:
        return candidate_prompt_path(root)
    return adopted_prompt_path(root)


def load_canonical_prompt(root: Path | None = None) -> str:
    return canonical_prompt_path(root).read_text(encoding="utf-8").strip()


def load_adopted_prompt(root: Path | None = None) -> str:
    return adopted_prompt_path(root).read_text(encoding="utf-8").strip()


def load_candidate_prompt(root: Path | None = None) -> str:
    return candidate_prompt_path(root).read_text(encoding="utf-8").strip()


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def canonical_prompt_sha256(root: Path | None = None) -> str:
    return prompt_sha256(load_canonical_prompt(root))


def adopted_prompt_sha256(root: Path | None = None) -> str:
    return prompt_sha256(load_adopted_prompt(root))


def candidate_prompt_sha256(root: Path | None = None) -> str:
    return prompt_sha256(load_candidate_prompt(root))
