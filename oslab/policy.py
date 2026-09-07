from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


class PolicyDenied(PermissionError):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


SECRET_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*|password\s*[:=]\s*)[^\s,;]+"
)


def redact(value: str) -> str:
    return SECRET_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", value)


def path_hash(path: Path) -> str:
    return hashlib.sha256(os.fsencode(str(path.resolve()))).hexdigest()


@dataclass(frozen=True)
class PathPolicy:
    allowed_roots: tuple[Path, ...]
    denied_roots: tuple[Path, ...]

    @classmethod
    def create(cls, allowed_roots: list[Path], denied_roots: list[Path]) -> PathPolicy:
        return cls(
            tuple(path.resolve() for path in allowed_roots),
            tuple(path.resolve() for path in denied_roots),
        )

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def authorize(self, requested: Path, *, write: bool = False, must_exist: bool = False) -> Path:
        if ".." in requested.parts:
            raise PolicyDenied("path_traversal", "parent traversal is forbidden")
        candidate = requested.resolve(strict=False)
        if must_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        if any(self._within(candidate, root) for root in self.denied_roots):
            raise PolicyDenied(
                "evaluator_immutable", "evaluator and hidden-test paths are immutable"
            )
        if not any(self._within(candidate, root) for root in self.allowed_roots):
            raise PolicyDenied("outside_allowed_root", "path is outside allowlisted roots")
        if write:
            self._reject_symlink_chain(candidate)
        return candidate

    def _reject_symlink_chain(self, path: Path) -> None:
        current = path
        while True:
            if current.exists() and current.is_symlink():
                raise PolicyDenied("symlink_escape", "writes through symlinks are forbidden")
            if current.parent == current:
                break
            current = current.parent


class CommandPolicy:
    def __init__(self, registry: dict[str, tuple[str, ...]]) -> None:
        self.registry = registry

    def resolve(self, profile: str, args: list[str]) -> list[str]:
        prefix = self.registry.get(profile)
        if prefix is None:
            raise PolicyDenied("command_not_allowlisted", f"unknown command profile: {profile}")
        for arg in args:
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise PolicyDenied(
                    "command_injection", "control characters in arguments are forbidden"
                )
        return [*prefix, *args]


def validate_qemu_network(arguments: list[str]) -> None:
    joined = " ".join(arguments).lower()
    forbidden = ("-net user", "-netdev user", "tap", "bridge", "socket", "passt", "slirp")
    if any(token in joined for token in forbidden):
        raise PolicyDenied(
            "guest_network", "public, NAT, bridged, and socket guest networking is forbidden"
        )
    if "-nic none" not in joined and "-net none" not in joined:
        raise PolicyDenied("guest_network", "QEMU must explicitly disable networking")


def detect_evaluator_exploit(diff_text: str) -> list[str]:
    patterns: dict[str, str] = {
        "evaluator modification": r"(?im)^diff --git a/(?:tests/hidden|\.oslab/evaluator|evaluator)/",
        "test disabling": r"(?i)(pytest\.mark\.skip|unittest\.skip|assertions?\s*=\s*false|--disable-assert)",
        "timeout inflation": r"(?i)(timeout.{0,20}(?:\*\s*10|99999|3600000))",
        "error swallowing": r"(?is)except\s+(?:Exception|BaseException)?\s*:\s*(?:pass|return\s+True)",
        "fake pass": r"(?i)(print|echo).{0,20}(?:OSLAB_EVT|event).{0,40}PASS",
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, diff_text)]
