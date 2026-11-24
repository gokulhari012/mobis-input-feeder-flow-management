"""Schema migrations and ORM / schema consistency."""

from __future__ import annotations

import pytest

from mobis.database.database import Database
from mobis.database.migrations import MigrationRunner, SchemaVersionError
from mobis.database.models import Base


def test_fresh_database_is_migrated_once(tmp_path):
    db = Database(tmp_path / "m.db")
    runner = MigrationRunner(db.engine)
    assert runner.current_version() == 0
    assert runner.upgrade() == [MigrationRunner.latest_version()]
    assert runner.upgrade() == []
    db.dispose()


def test_orm_columns_exist_in_migrated_schema(db):
    with db.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            columns = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            assert columns, f"table {table.name} missing"
            missing = {c.name for c in table.columns} - columns
            assert not missing, f"{table.name}: ORM columns missing in schema: {missing}"


def test_database_newer_than_application_is_refused(db):
    with db.engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO schema_migrations VALUES (999, 'future', '2030-01-01T00:00:00+00:00')")
    with pytest.raises(SchemaVersionError):
        MigrationRunner(db.engine).upgrade()


def test_backup_hook_called_before_upgrading_existing_database(tmp_path, monkeypatch):
    from mobis.database.migrations import runner as runner_module

    db = Database(tmp_path / "m.db")
    MigrationRunner(db.engine).upgrade()
    calls = []
    extra = runner_module.Migration(99, "extra", ("CREATE TABLE extra_table (id INTEGER PRIMARY KEY)",))
    monkeypatch.setattr(runner_module, "load_migrations",
                        lambda: [runner_module.Migration(1, "initial_schema", ()), extra])
    applied = MigrationRunner(db.engine, before_upgrade=lambda cur, tgt: calls.append((cur, tgt))).upgrade()
    assert applied == [99] and calls == [(1, 99)]
    db.dispose()


def test_foreign_keys_and_wal_enabled(db):
    raw = db.engine.raw_connection()
    try:
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        raw.close()
