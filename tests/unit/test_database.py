from pathlib import Path

from oslab.database import LabDatabase


def test_migration_restart_backup_restore(tmp_path: Path) -> None:
    database = LabDatabase(tmp_path / "lab.sqlite3")
    assert database.migrate() == 2
    assert database.migrate() == 2
    assert database.integrity_check()["ok"]
    backup = database.backup(tmp_path / "backup.sqlite3")
    database.restore(backup)
    assert database.integrity_check()["migrations"] == 2
