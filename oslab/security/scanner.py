from __future__ import annotations

import hashlib
import io
import os
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from uuid import uuid4

from oslab.security.models import (
    Evidence,
    FindingStatus,
    SecurityFinding,
    Severity,
    SourceLocation,
    ValidationVerdict,
)

MAX_FILE_BYTES = 2_000_000
IGNORED_PARTS = {
    ".git",
    ".oslab",
    ".venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "linked-repositories",
}
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".ts",
    ".tsx",
}
RECOGNIZED_INPUT = re.compile(
    r"\b(?:request\.|req\.|sys\.argv|os\.environ|input\s*\(|stdin|query_params|form_data)"
)


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: Severity
    cwe: str
    suffixes: frozenset[str]
    pattern: Pattern[str]
    description: str


RULES = (
    Rule(
        "PY-CMD-001",
        "Dynamic command passed to os.system",
        Severity.HIGH,
        "CWE-78",
        frozenset({".py"}),
        re.compile(r"\bos\.system\s*\(\s*(?![rubfRUBF]*['\"])[^)]+\)"),
        "A non-literal command reaches a shell execution API.",
    ),
    Rule(
        "PY-CMD-002",
        "Subprocess invocation enables a shell",
        Severity.HIGH,
        "CWE-78",
        frozenset({".py"}),
        re.compile(
            r"\bsubprocess\.(?:run|Popen|call|check_call|check_output)\s*\(.*shell\s*=\s*True"
        ),
        "Shell parsing is enabled for a subprocess call and may make data-to-command boundaries unsafe.",
    ),
    Rule(
        "PY-DESER-001",
        "Potentially unsafe pickle deserialization",
        Severity.HIGH,
        "CWE-502",
        frozenset({".py"}),
        re.compile(r"\b(?:pickle|cPickle)\.loads?\s*\("),
        "Pickle deserialization can execute code when the payload is attacker controlled.",
    ),
    Rule(
        "PY-YAML-001",
        "Unsafe YAML loader",
        Severity.HIGH,
        "CWE-502",
        frozenset({".py"}),
        re.compile(r"\byaml\.load\s*\((?!.*(?:SafeLoader|safe_load))"),
        "yaml.load is used without an evident safe loader.",
    ),
    Rule(
        "JS-CMD-001",
        "Dynamic child-process execution",
        Severity.HIGH,
        "CWE-78",
        frozenset({".js", ".jsx", ".ts", ".tsx"}),
        re.compile(r"\b(?:exec|execSync)\s*\(\s*(?:`|[A-Za-z_$])"),
        "A dynamic value appears to reach a shell-oriented child-process API.",
    ),
    Rule(
        "CODE-SQL-001",
        "SQL query constructed with interpolation",
        Severity.HIGH,
        "CWE-89",
        frozenset(TEXT_SUFFIXES),
        re.compile(r"(?i)\b(?:execute|query)\s*\(\s*(?:f['\"]|`|[^,]*(?:\+|\.format\())"),
        "An SQL execution API appears to receive an interpolated query.",
    ),
)


def discover_files(repository: Path, *, max_files: int) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(repository, followlinks=False):
        names[:] = sorted(name for name in names if name not in IGNORED_PARTS)
        for filename in sorted(filenames):
            if len(files) >= max_files:
                return files
            path = Path(directory) / filename
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def scan_repository(repository: Path, audit_id: str, *, max_files: int) -> list[SecurityFinding]:
    findings: dict[str, SecurityFinding] = {}
    for path in discover_files(repository, max_files=max_files):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(repository).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//", "*")):
                continue
            for rule in RULES:
                match = rule.pattern.search(line)
                if (
                    path.suffix.lower() not in rule.suffixes
                    or match is None
                    or (
                        path.suffix.lower() == ".py"
                        and not _python_match_is_code(line, match.start())
                    )
                ):
                    continue
                normalized = re.sub(r"\s+", " ", stripped)
                fingerprint = hashlib.sha256(
                    f"{rule.id}\0{relative}\0{normalized}".encode()
                ).hexdigest()
                snippet_hash = hashlib.sha256(line.encode()).hexdigest()
                status, verdict = _validate(rule, relative, stripped)
                findings[fingerprint] = SecurityFinding(
                    id=f"finding-{uuid4().hex[:16]}",
                    audit_id=audit_id,
                    fingerprint=fingerprint,
                    status=status,
                    severity=rule.severity,
                    title=rule.title,
                    description=rule.description,
                    rule_id=rule.id,
                    cwe=rule.cwe,
                    locations=[
                        SourceLocation(
                            path=relative,
                            line=line_number,
                            snippet_hash=snippet_hash,
                        )
                    ],
                    reachability=(
                        "Static sink identified; caller-controlled reachability requires review."
                        if status == FindingStatus.NEEDS_REVIEW
                        else "A recognized request, CLI, or environment input flows directly to the sink."
                    ),
                    confidence=0.92 if status == FindingStatus.CONFIRMED else 0.68,
                    evidence=[
                        Evidence(
                            kind="static_trace",
                            summary=f"Rule {rule.id} matched {relative}:{line_number}.",
                            deterministic=True,
                        ),
                        Evidence(
                            kind="adversarial_review",
                            summary=verdict.reason,
                            deterministic=True,
                        ),
                    ],
                    validators=[verdict],
                    provenance={"engine": "cyntox-native-static", "rule_version": 1},
                )
    return list(findings.values())


def _python_match_is_code(line: str, column: int) -> bool:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(line).readline)
        return not any(
            token.type in {tokenize.STRING, tokenize.COMMENT}
            and token.start[1] <= column < token.end[1]
            for token in tokens
        )
    except (IndentationError, tokenize.TokenError):
        return True


def _validate(rule: Rule, relative: str, line: str) -> tuple[FindingStatus, ValidationVerdict]:
    test_path = any(
        part in {"test", "tests", "fixtures", "examples"} for part in Path(relative).parts
    )
    if test_path:
        return FindingStatus.REJECTED, ValidationVerdict(
            validator="cyntox-deterministic-adversary",
            verdict="reject",
            reason="The match is in test, fixture, or example code and is not reported as production reachability.",
        )
    if rule.id == "PY-YAML-001" and "Loader=" not in line and RECOGNIZED_INPUT.search(line):
        return FindingStatus.CONFIRMED, ValidationVerdict(
            validator="cyntox-deterministic-adversary",
            verdict="accept",
            reason="A recognized external input is passed to yaml.load without an explicit safe loader.",
        )
    if rule.id == "PY-CMD-001" and RECOGNIZED_INPUT.search(line):
        return FindingStatus.CONFIRMED, ValidationVerdict(
            validator="cyntox-deterministic-adversary",
            verdict="accept",
            reason="A recognized external input is passed directly to os.system.",
        )
    return FindingStatus.NEEDS_REVIEW, ValidationVerdict(
        validator="cyntox-deterministic-adversary",
        verdict="needs_review",
        reason="The sink is real, but attacker-controlled reachability cannot be proven from one line.",
    )
