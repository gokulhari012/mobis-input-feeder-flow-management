"""MES synchronisation worker (lives in its own QThread; HTTP never blocks the GUI)."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from mobis.core.config_models import MesConfig
from mobis.integrations.mes_client import MesClient, MesResponse
from mobis.integrations.mes_sync_manager import MesSyncManager, SyncReport

log = logging.getLogger("mobis.mes")


class MesSyncWorker(QObject):
    report_ready = Signal(object)  # SyncReport
    test_result = Signal(object)   # MesResponse

    def __init__(self, manager: MesSyncManager):
        super().__init__()
        self._manager = manager
        self._timer: Optional[QTimer] = None
        self._running = False

    def _interval_ms(self) -> int:
        return max(5, int(self._manager.config.sync_interval_s)) * 1000

    @Slot()
    def start(self) -> None:
        try:
            self._manager.recover()
        except Exception:  # noqa: BLE001
            log.exception("MES queue recovery failed")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run_scheduled)
        self._timer.start(3000)  # first cycle shortly after start-up

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    @Slot(object, str)
    def apply_config(self, config: MesConfig, station_code: str) -> None:
        self._manager.apply_config(config, station_code)
        if self._timer is not None and not self._running:
            self._timer.start(min(self._interval_ms(), 5000))

    @Slot()
    def sync_now(self) -> None:
        self._run(force=True)

    @Slot(object)
    def test(self, config: MesConfig) -> None:
        try:
            result = MesClient(config).test_connection()
        except Exception as exc:  # noqa: BLE001
            result = MesResponse(False, None, reachable=False, error=str(exc))
        self.test_result.emit(result)

    def _run_scheduled(self) -> None:
        self._run(force=False)

    def _run(self, force: bool) -> None:
        if self._running:
            return
        self._running = True
        try:
            report = self._manager.run_once(force=force)
        except Exception as exc:  # noqa: BLE001 - never let the worker die
            log.exception("MES synchronisation cycle failed")
            report = SyncReport(error=str(exc))
        finally:
            self._running = False
        self.report_ready.emit(report)
        if self._timer is not None:
            self._timer.start(self._interval_ms())
