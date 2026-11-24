"""GUI-thread facade for MES synchronisation (owns the MES worker thread)."""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

from mobis.core.models import Severity
from mobis.database.database import Database
from mobis.database.repositories import Repositories
from mobis.utils.timeutil import utc_now
from mobis.workers.mes_sync_worker import MesSyncWorker

from .mes_payload import enqueue_transaction
from .mes_sync_manager import MesSyncManager, SyncReport

log = logging.getLogger("mobis.mes")


class MesSyncController(QObject):
    status_changed = Signal()

    _sig_sync_now = Signal()
    _sig_apply = Signal(object, str)
    _sig_test = Signal(object)
    _sig_stop = Signal()

    def __init__(self, db: Database, repos: Repositories, config_service, events, hub):
        super().__init__()
        self._db = db
        self._repos = repos
        self._config_service = config_service
        self._events = events
        self._hub = hub
        self.online: Optional[bool] = None
        self.last_success_at: Optional[datetime] = None
        self.last_attempt_at: Optional[datetime] = None
        self.last_error = ""
        self.last_report: Optional[SyncReport] = None
        self.counts: dict[str, int] = {}
        self._thread: Optional[QThread] = None
        self._worker: Optional[MesSyncWorker] = None
        self._test_callbacks: list[Callable] = []
        config_service.changed.connect(self._on_config_changed)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        app = self._config_service.app()
        manager = MesSyncManager(self._db, self._repos, self._config_service.mes(), app.station_code)
        self._thread = QThread()
        self._thread.setObjectName("MES-Worker")
        worker = MesSyncWorker(manager)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.start)
        worker.report_ready.connect(self._on_report)
        worker.test_result.connect(self._on_test_result)
        self._sig_sync_now.connect(worker.sync_now)
        self._sig_apply.connect(worker.apply_config)
        self._sig_test.connect(worker.test)
        self._sig_stop.connect(worker.stop, Qt.ConnectionType.BlockingQueuedConnection)
        self._worker = worker
        self._thread.start()
        self.refresh_counts()

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread.isRunning():
            self._sig_stop.emit()
        self._thread.quit()
        if not self._thread.wait(15000):
            log.warning("MES worker did not stop within 15 s")
        self._thread = None
        self._worker = None

    @property
    def worker_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    # ------------------------------------------------------------------ API
    def enqueue_callable(self) -> Callable:
        """Function used by the transaction manager inside its unit of work."""
        return lambda session, tx: enqueue_transaction(session, self._repos, tx, self._config_service.mes())

    def sync_now(self) -> None:
        self._sig_sync_now.emit()

    def test_connection(self, config, callback: Callable) -> None:
        self._test_callbacks.append(callback)
        self._sig_test.emit(copy.deepcopy(config))

    def refresh_counts(self) -> dict[str, int]:
        try:
            with self._db.read_session() as s:
                self.counts = self._repos.mes_queue.counts(s)
                if self.last_success_at is None:
                    self.last_success_at = self._repos.mes_queue.last_synced_at(s)
        except Exception:  # noqa: BLE001
            log.exception("Could not read MES queue counts")
        self.status_changed.emit()
        return self.counts

    def retry_failed(self, ids: Optional[list[int]] = None) -> int:
        with self._db.session() as s:
            count = self._repos.mes_queue.retry_failed(s, ids)
        if count:
            self._events.log(Severity.INFO, "MES", "RETRY_FAILED", f"{count} failed MES record(s) queued for retry")
        self.refresh_counts()
        self._hub.mes_queue_changed.emit()
        self.sync_now()
        return count

    def status(self) -> tuple[str, str]:
        """``(level, text)`` for status indicators."""
        cfg = self._config_service.mes()
        if not cfg.enabled:
            return "neutral", "Disabled"
        if not cfg.base_url.strip():
            return "warn", "Not configured"
        if self.online is None:
            return "info", "Waiting"
        return ("ok", "Online") if self.online else ("ng", "Offline")

    # ------------------------------------------------------------------ callbacks
    def _on_report(self, report: SyncReport) -> None:
        self.last_report = report
        if report.skipped_reason:
            self.online = None
        else:
            self.last_attempt_at = utc_now()
            if report.online is not None:
                if report.online != self.online:
                    if report.online:
                        self._events.log(Severity.INFO, "MES", "MES_ONLINE", "MES server reachable")
                    else:
                        self._events.log(Severity.WARNING, "MES", "MES_OFFLINE",
                                         f"MES server unreachable: {report.error}")
                self.online = report.online
            if report.synced:
                self.last_success_at = utc_now()
            if report.error:
                self.last_error = report.error
            if report.failed:
                self._events.log(Severity.ERROR, "MES", "MES_RECORDS_FAILED",
                                 f"{report.failed} record(s) reached the maximum retry count: {report.error}")
        self.refresh_counts()
        if report.attempted:
            self._hub.mes_queue_changed.emit()

    def _on_test_result(self, result) -> None:
        if self._test_callbacks:
            callback = self._test_callbacks.pop(0)
            try:
                callback(result)
            except Exception:  # noqa: BLE001
                log.exception("MES test callback failed")

    def _on_config_changed(self, section: str) -> None:
        if section in ("mes", "app"):
            self._sig_apply.emit(self._config_service.mes(), self._config_service.app().station_code)
            if section == "mes":
                self.online = None
            self.status_changed.emit()
