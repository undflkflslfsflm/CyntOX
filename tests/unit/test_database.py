# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from pathlib import Path

from oslab.database import LabDatabase
from oslab.database.db import MIGRATIONS


def test_migration_restart_backup_restore(tmp_path: Path) -> None:
    database = LabDatabase(tmp_path / "lab.sqlite3")
    assert database.migrate() == len(MIGRATIONS)
    assert database.migrate() == len(MIGRATIONS)
    assert database.integrity_check()["ok"]
    backup = database.backup(tmp_path / "backup.sqlite3")
    database.restore(backup)
    assert database.integrity_check()["migrations"] == len(MIGRATIONS)
