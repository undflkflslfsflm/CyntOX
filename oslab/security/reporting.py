from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oslab.security.models import AuditRun, SecurityFinding


def write_reports(output: Path, audit: AuditRun, findings: list[SecurityFinding]) -> dict[str, str]:
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "audit": audit.model_dump(mode="json"),
        "summary": _summary(findings),
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
    json_path = output / "findings.json"
    markdown_path = output / "REPORT.md"
    sarif_path = output / "findings.sarif"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(audit, findings), encoding="utf-8")
    sarif_path.write_text(
        json.dumps(_sarif(findings), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "json": str(json_path.resolve()),
        "markdown": str(markdown_path.resolve()),
        "sarif": str(sarif_path.resolve()),
    }


def _summary(findings: list[SecurityFinding]) -> dict[str, Any]:
    return {
        "total": len(findings),
        "by_status": {
            status: sum(1 for finding in findings if finding.status == status)
            for status in ("confirmed", "needs_review", "rejected")
        },
        "by_severity": {
            severity: sum(1 for finding in findings if finding.severity == severity)
            for severity in ("critical", "high", "medium", "low", "info")
        },
    }


def _markdown(audit: AuditRun, findings: list[SecurityFinding]) -> str:
    visible = [finding for finding in findings if finding.status != "rejected"]
    lines = [
        "# CyntOX Security Audit",
        "",
        f"- Audit: `{audit.id}`",
        f"- Commit: `{audit.base_commit}`",
        f"- Profile: `{audit.profile}`",
        f"- Confirmed: {sum(1 for finding in findings if finding.status == 'confirmed')}",
        f"- Needs review: {sum(1 for finding in findings if finding.status == 'needs_review')}",
        "",
    ]
    if not visible:
        lines.extend(["No confirmed or review-required findings were produced.", ""])
    for finding in visible:
        location = finding.locations[0]
        lines.extend(
            [
                f"## {finding.severity.upper()}: {finding.title}",
                "",
                f"- Status: `{finding.status}`",
                f"- Rule: `{finding.rule_id}` ({finding.cwe or 'unmapped'})",
                f"- Location: `{location.path}:{location.line}`",
                f"- Confidence: {finding.confidence:.2f}",
                "",
                finding.description,
                "",
                f"Reachability: {finding.reachability}",
                "",
            ]
        )
        if finding.patch is not None:
            lines.extend(
                [
                    f"Patch candidate: `{finding.patch.diff_sha256}` "
                    f"(verified: `{str(finding.patch.verified).lower()}`)",
                    "",
                ]
            )
    return "\n".join(lines)


def _sarif(findings: list[SecurityFinding]) -> dict[str, Any]:
    visible = [finding for finding in findings if finding.status != "rejected"]
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for finding in visible:
        rules[finding.rule_id] = {
            "id": finding.rule_id,
            "name": finding.title,
            "shortDescription": {"text": finding.description},
            "properties": {"security-severity": _sarif_score(str(finding.severity))},
        }
        location = finding.locations[0]
        results.append(
            {
                "ruleId": finding.rule_id,
                "level": _sarif_level(str(finding.severity)),
                "message": {"text": f"{finding.title}: {finding.reachability}"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": location.path},
                            "region": {
                                "startLine": location.line,
                                "startColumn": location.column,
                            },
                        }
                    }
                ],
                "fingerprints": {"cyntox/v1": finding.fingerprint},
                "properties": {
                    "status": finding.status,
                    "confidence": finding.confidence,
                    "cwe": finding.cwe,
                },
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "CyntOX Native Security Audit",
                        "version": "0.1.0",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def _sarif_level(severity: str) -> str:
    return {"critical": "error", "high": "error", "medium": "warning"}.get(severity, "note")


def _sarif_score(severity: str) -> str:
    return {
        "critical": "9.5",
        "high": "8.0",
        "medium": "5.5",
        "low": "2.5",
        "info": "0.0",
    }[severity]
