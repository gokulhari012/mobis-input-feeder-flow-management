"""Database backup and restore.

Backups use SQLite's online backup API, so they are consistent even while the
application is writing.  A restore is *staged*: the selected backup is
validated and copied to ``pending_restore.db``; it is applied at the next
application start, before the database is opened (after taking a safety
backup of the current database).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from mobis.database.migrations import MigrationRunner
from mobis.utils.timeutil import local_stamp

from .paths import AppPaths

log = logging.getLogger("mobis.backup")

REQUIRED_TABLES = {"schema_migrations", "material_transactions", "equipment", "bag_types"}


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    size_bytes: int
    modified: datetime

    @property
    def name(self) -> str:
        return self.path.name


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(destination))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def validate_backup_file(path: Path) -> tuple[bool, str]:
    """Check that ``path`` is an intact MOBIS database this version can open."""
    if not path.exists():
        return False, "File not found."
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return False, f"Not a valid SQLite database: {exc}"
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            return False, f"Integrity check failed: {result[0] if result else 'no result'}"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            return False, f"Not a MOBIS database (missing tables: {', '.join(sorted(missing))})."
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        latest = MigrationRunner.latest_version()
        if version > latest:
            return False, f"Backup schema version {version} is newer than this application ({latest})."
        count = conn.execute("SELECT COUNT(*) FROM material_transactions").fetchone()[0]
        return True, f"Schema version {version}, {count} transactions."
    except sqlite3.Error as exc:
        return False, f"Cannot read database: {exc}"
    finally:
        conn.close()


class BackupService:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def create_backup(self, reason: str = "manual") -> Path:
        safe_reason = "".join(ch for ch in reason if ch.isalnum() or ch in "-_") or "manual"
        destination = self.paths.backups_dir / f"mobis_{local_stamp()}_{safe_reason}.db"
        _sqlite_backup(self.paths.db_file, destination)
        log.info("Database backup created: %s", destination)
        return destination

    def list_backups(self) -> list[BackupInfo]:
        if not self.paths.backups_dir.exists():
            return []
        items = []
        for path in self.paths.backups_dir.glob("*.db"):
            stat = path.stat()
            items.append(BackupInfo(path, stat.st_size, datetime.fromtimestamp(stat.st_mtime)))
        return sorted(items, key=lambda b: b.modified, reverse=True)

    def prune_auto_backups(self, keep: int) -> int:
        autos = [b for b in self.list_backups() if b.path.stem.endswith("_auto")]
        removed = 0
        for backup in autos[max(1, keep):]:
            try:
                backup.path.unlink()
                removed += 1
            except OSError as exc:
                log.warning("Could not delete old backup %s: %s", backup.path, exc)
        return removed

    def last_auto_backup(self) -> Optional[datetime]:
        autos = [b.modified for b in self.list_backups() if b.path.stem.endswith("_auto")]
        return max(autos) if autos else None

    def schedule_restore(self, backup_path: Path) -> str:
        ok, message = validate_backup_file(backup_path)
        if not ok:
            raise ValueError(message)
        shutil.copy2(backup_path, self.paths.pending_restore_file)
        marker = self.paths.pending_restore_file.with_suffix(".json")
        marker.write_text(json.dumps({"source": str(backup_path), "requested_at": local_stamp()}), encoding="utf-8")
        log.warning("Database restore scheduled from %s", backup_path)
        return message

    @staticmethod
    def apply_pending_restore(paths: AppPaths) -> Optional[str]:
        """Apply a staged restore.  Must run before the database is opened."""
        pending = paths.pending_restore_file
        if not pending.exists():
            return None
        marker = pending.with_suffix(".json")
        source = ""
        if marker.exists():
            try:
                source = json.loads(marker.read_text(encoding="utf-8")).get("source", "")
            except (OSError, ValueError):
                source = ""
        ok, message = validate_backup_file(pending)
        if not ok:
            log.error("Staged restore rejected: %s", message)
            pending.rename(pending.with_name(f"rejected_restore_{local_stamp()}.db"))
            marker.unlink(missing_ok=True)
            return f"Staged database restore was rejected: {message}"
        if paths.db_file.exists():
            safety = paths.backups_dir / f"mobis_{local_stamp()}_pre_restore.db"
            _sqlite_backup(paths.db_file, safety)
            for suffix in ("", "-wal", "-shm"):
                Path(str(paths.db_file) + suffix).unlink(missing_ok=True)
        shutil.move(str(pending), str(paths.db_file))
        marker.unlink(missing_ok=True)
        log.warning("Database restored from %s", source or pending)
        return f"Database restored from backup {Path(source).name if source else ''} ({message})"
