"""SQLite engine and session management.

* WAL journal mode: readers (history screen, MES worker) never block the writer.
* ``synchronous=FULL``: committed traceability records survive a power loss.
* Write sessions start with ``BEGIN IMMEDIATE`` so that read-modify-write
  sequences (inventory update, MES queue claim) acquire the write lock up front
  and can never fail half way with ``SQLITE_BUSY``.
* Every unit of work runs in a single database transaction: a loading
  transaction, its inventory update and its MES queue entry are committed
  together or not at all.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger("mobis.database")


def _configure_sqlite(engine: Engine, begin_statement: str) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # pragma: no cover - exercised implicitly
        # Disable pysqlite's implicit transaction handling; we emit BEGIN ourselves.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection):  # pragma: no cover - exercised implicitly
        connection.exec_driver_sql(begin_statement)


class Database:
    def __init__(self, db_path: Path, echo: bool = False):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{self.path.as_posix()}"
        connect_args = {"check_same_thread": False, "timeout": 15}
        self.engine = create_engine(url, connect_args=connect_args, echo=echo)
        _configure_sqlite(self.engine, "BEGIN IMMEDIATE")
        self.read_engine = create_engine(url, connect_args=connect_args, echo=echo)
        _configure_sqlite(self.read_engine, "BEGIN")
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._read_factory = sessionmaker(bind=self.read_engine, expire_on_commit=False)

    # ------------------------------------------------------------------ #
    @contextmanager
    def session(self) -> Iterator[Session]:
        """Write unit of work: commits on success, rolls back on any exception."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        """Read-only unit of work.

        Only ``close()`` is called (no explicit rollback, which would expire the
        loaded objects): the pool's reset-on-return ends the read transaction
        and returned objects stay usable as detached, fully loaded instances.
        """
        session = self._read_factory()
        try:
            yield session
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    def health_check(self) -> tuple[bool, str]:
        if not self.path.exists():
            return False, f"Database file missing: {self.path}"
        try:
            with self.read_engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1").fetchone()
            return True, "OK"
        except Exception as exc:  # noqa: BLE001 - report any driver error
            log.error("Database health check failed: %s", exc)
            return False, str(exc)

    def _raw_query(self, sql: str) -> list[tuple]:
        raw = self.engine.raw_connection()
        try:
            cursor = raw.cursor()
            try:
                cursor.execute(sql)
                return cursor.fetchall()
            finally:
                cursor.close()
        finally:
            raw.close()

    def integrity_check(self) -> str:
        rows = self._raw_query("PRAGMA integrity_check")
        return ", ".join(str(r[0]) for r in rows)

    def checkpoint(self) -> None:
        try:
            self._raw_query("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # noqa: BLE001
            log.warning("WAL checkpoint failed: %s", exc)

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def dispose(self) -> None:
        self.checkpoint()
        self.engine.dispose()
        self.read_engine.dispose()
