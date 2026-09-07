# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from oslab.config import default_config
from oslab.database import LabDatabase
from oslab.security import AuditProfile, SecurityAuditStore, audit_plan, create_audit, run_audit
from oslab.security.adapters import load_sarif
from oslab.security.scanner import scan_repository


def _git(root: Path, *args: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    result = subprocess.run(  # noqa: S603 - resolved test executable
        [executable, *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    _git(root, "init")
    (root / "app.py").write_text(
        'import yaml\n\nmarker = "yaml.load("\n\ndef parse(request):\n    return yaml.load(request.data)\n',
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_example.py").write_text(
        "import os\n\ndef test_fixture():\n    os.system(command)\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=CyntOX Test",
        "-c",
        "user.email=cyntox@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    return root


def test_scanner_validates_production_and_rejects_test_match(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    findings = scan_repository(root, "audit-test", max_files=100)

    by_rule = {finding.rule_id: finding for finding in findings}
    assert by_rule["PY-YAML-001"].status == "confirmed"
    assert by_rule["PY-CMD-001"].status == "rejected"
    assert by_rule["PY-YAML-001"].locations[0].path == "app.py"


def test_standard_audit_is_resumable_and_preserves_source(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    config = default_config(tmp_path / "controller")
    config.project_root.mkdir()
    original = (root / "app.py").read_bytes()

    audit = create_audit(config, root, AuditProfile.STANDARD)
    completed = run_audit(config, audit.id)
    repeated = run_audit(config, audit.id)

    assert completed.state == "complete"
    assert repeated.state == "complete"
    assert (root / "app.py").read_bytes() == original
    findings = SecurityAuditStore(LabDatabase(config.runtime_root / "oslab.sqlite3")).findings(
        audit.id
    )
    confirmed = next(finding for finding in findings if finding.status == "confirmed")
    assert confirmed.patch is not None
    assert confirmed.patch.verified is True
    assert "yaml.safe_load" in confirmed.patch.diff
    assert load_sarif(Path(completed.report_paths["sarif"]))["version"] == "2.1.0"
    report = json.loads(Path(completed.report_paths["json"]).read_text(encoding="utf-8"))
    assert report["summary"]["by_status"]["confirmed"] == 1


def test_standard_blocks_dirty_but_quick_allows_read_only_plan(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "app.py").write_text("changed = True\n", encoding="utf-8")

    assert audit_plan(root, AuditProfile.QUICK)["readiness"] == "ready"
    assert audit_plan(root, AuditProfile.STANDARD)["readiness"] == "blocked-dirty-worktree"
    with pytest.raises(ValueError, match="clean source worktree"):
        create_audit(default_config(tmp_path / "controller"), root, AuditProfile.STANDARD)


def test_malformed_sarif_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.sarif"
    path.write_text('{"version":"1.0"}', encoding="utf-8")

    with pytest.raises(ValueError, match="SARIF 2.1.0"):
        load_sarif(path)
