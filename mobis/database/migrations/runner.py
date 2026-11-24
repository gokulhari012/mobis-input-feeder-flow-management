"""Minimal, transactional migration runner for the embedded SQLite database."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import import_module
from typing import Callable, Optional

from sqlalchemy.engine import Engine

from mobis.utils.timeutil import to_iso, utc_now

log = logging.getLogger("mobis.database.migrations")

#: Ordered list of migration modules (inside this package).
MIGRATION_MODULES = [
    "m0001_initial_schema",
]


class SchemaVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


def load_migrations() -> list[Migration]:
    migrations = []
    for module_name in MIGRATION_MODULES:
        module = import_module(f"{__package__}.{module_name}")
        migrations.append(Migration(module.VERSION, module.NAME, tuple(module.STATEMENTS)))
    versions = [m.version for m in migrations]
    if versions != sorted(set(versions)):
        raise SchemaVersionError(f"Migration versions must be unique and ascending: {versions}")
    return migrations


_CREATE_VERSION_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
)


class MigrationRunner:
    def __init__(self, engine: Engine, before_upgrade: Optional[Callable[[int, int], None]] = None):
        self.engine = engine
        self.before_upgrade = before_upgrade

    def current_version(self) -> int:
        with self.engine.begin() as conn:
            conn.exec_driver_sql(_CREATE_VERSION_TABLE)
            row = conn.exec_driver_sql("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    @staticmethod
    def latest_version() -> int:
        migrations = load_migrations()
        return migrations[-1].version if migrations else 0

    def upgrade(self) -> list[int]:
        """Apply all pending migrations; returns the versions applied."""
        current = self.current_version()
        migrations = load_migrations()
        latest = migrations[-1].version if migrations else 0
        if current > latest:
            raise SchemaVersionError(
                f"Database schema version {current} is newer than this application supports ({latest}). "
                "Install the matching or a newer application version."
            )
        pending = [m for m in migrations if m.version > current]
        if not pending:
            return []
        if current > 0 and self.before_upgrade is not None:
            self.before_upgrade(current, pending[-1].version)
        applied = []
        for migration in pending:
            log.info("Applying database migration %04d %s", migration.version, migration.name)
            with self.engine.begin() as conn:
                for statement in migration.statements:
                    conn.exec_driver_sql(statement)
                conn.exec_driver_sql(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, to_iso(utc_now())),
                )
            applied.append(migration.version)
        return applied
