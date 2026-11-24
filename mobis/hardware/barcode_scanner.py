"""Barcode scanner integration.

Three input paths, selected in the scanner configuration:

* ``HID_KEYBOARD`` - keyboard-wedge scanners (most wireless USB dongles).  The
  operation screen keeps a dedicated scan field focused, measures the
  inter-key timing (scanner bursts are much faster than human typing) and
  submits on the configured terminator.
* ``SERIAL``       - scanners / cradles configured as (virtual) COM port.
* ``SIMULATION``   - scans injected from the simulation panel.

All inputs pass through the same normaliser (prefix / suffix removal, control
characters, length limits) and duplicate-scan protection.
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

from mobis.core.barcode_parser import BarcodeNormalizer
from mobis.core.config_models import ScannerConfig
from mobis.core.models import InputSource, Severity
from mobis.utils.timeutil import utc_now
from mobis.workers.scanner_worker import SerialScannerWorker

log = logging.getLogger("mobis.scanner")


def list_serial_ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, p.description or p.device) for p in sorted(list_ports.comports(), key=lambda p: p.device)]


def classify_keyboard_input(key_count: int, avg_interval_ms: float, max_interkey_ms: int) -> InputSource:
    """Scanner bursts arrive much faster than human typing."""
    if key_count >= 2 and avg_interval_ms <= max_interkey_ms:
        return InputSource.SCANNER_HID
    return InputSource.MANUAL


class ScannerManager(QObject):
    barcode_scanned = Signal(str, str)   # barcode, input source
    test_scan = Signal(str, str, str)    # barcode, input source, raw input (test mode)
    rejected = Signal(str, str)          # raw input, reason
    status_changed = Signal()

    _sig_apply = Signal(object)
    _sig_stop = Signal()

    def __init__(self, config_service, events, force_simulation: bool = False):
        super().__init__()
        self._config_service = config_service
        self._events = events
        self._force_sim = force_simulation
        self._cfg: ScannerConfig = config_service.scanner()
        self._normalizer = BarcodeNormalizer(self._cfg)
        self.state = "STOPPED"
        self.detail = ""
        self.last_barcode = ""
        self.last_source = ""
        self.last_time: Optional[datetime] = None
        self.test_mode = False
        self._last_accepted: tuple[str, float] = ("", 0.0)
        self._thread: Optional[QThread] = None
        self._worker: Optional[SerialScannerWorker] = None
        config_service.changed.connect(self._on_config_changed)

    # ------------------------------------------------------------------ properties
    @property
    def config(self) -> ScannerConfig:
        return copy.deepcopy(self._cfg)

    @property
    def mode(self) -> str:
        return "SIMULATION" if self._force_sim else self._cfg.mode

    @property
    def simulation_enabled(self) -> bool:
        return self.mode == "SIMULATION"

    def status(self) -> tuple[str, str]:
        """``(level, text)`` for status indicators."""
        if self.mode == "SIMULATION":
            return "info", "Simulation"
        if self.mode == "SERIAL":
            if self.state == "CONNECTED":
                return "ok", f"{self._cfg.serial_port} connected"
            return "ng", "Disconnected"
        return "ok", "HID ready"

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        mode = self.mode
        if mode == "SERIAL":
            self._start_serial()
        elif mode == "SIMULATION":
            self._set_state("SIMULATION", "Scanner simulation active - use the simulation panel on the dashboard.")
        else:
            self._set_state("READY", "Keyboard-wedge (HID) mode: scans are captured by the scan field "
                                     "(connection status is not reported by HID devices).")

    def stop(self) -> None:
        if self._thread is not None:
            if self._thread.isRunning():
                self._sig_stop.emit()
            self._thread.quit()
            self._thread.wait(3000)
            try:
                self._sig_apply.disconnect()
                self._sig_stop.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._thread = None
        self._worker = None
        self._set_state("STOPPED", "")

    def _start_serial(self) -> None:
        self._thread = QThread()
        self._thread.setObjectName("Scanner-Worker")
        worker = SerialScannerWorker(copy.deepcopy(self._cfg))
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.start)
        worker.data_received.connect(self._on_serial_data)
        worker.status_changed.connect(self._on_serial_status)
        self._sig_apply.connect(worker.apply_config)
        self._sig_stop.connect(worker.stop, Qt.ConnectionType.BlockingQueuedConnection)
        self._worker = worker
        self._set_state("CONNECTING", f"Opening {self._cfg.serial_port or '(no port configured)'}…")
        self._thread.start()

    def _set_state(self, state: str, detail: str) -> None:
        self.state = state
        self.detail = detail
        self.status_changed.emit()

    # ------------------------------------------------------------------ inputs
    def submit_keyboard(self, text: str, avg_interval_ms: float, key_count: int) -> None:
        source = classify_keyboard_input(key_count, avg_interval_ms, self._cfg.hid_max_interkey_ms)
        if source == InputSource.MANUAL and not self._cfg.allow_manual_entry and not self.test_mode:
            self.rejected.emit(text, "Manual barcode entry is disabled - please use the scanner.")
            return
        self._process(text, source)

    def submit_simulated(self, text: str) -> None:
        self._process(text, InputSource.SIMULATION)

    def _on_serial_data(self, raw: str) -> None:
        self._process(raw, InputSource.SCANNER_SERIAL)

    def _process(self, raw: str, source: InputSource) -> None:
        barcode, error = self._normalizer.normalize(raw)
        if barcode is None:
            self.rejected.emit(raw, error)
            return
        now = time.monotonic()
        last_code, last_time = self._last_accepted
        if (
            not self.test_mode
            and barcode == last_code
            and (now - last_time) * 1000.0 < self._cfg.duplicate_window_ms
        ):
            self.rejected.emit(raw, "Duplicate scan ignored.")
            return
        self._last_accepted = (barcode, now)
        self.last_barcode = barcode
        self.last_source = source.value
        self.last_time = utc_now()
        if self.test_mode:
            self.test_scan.emit(barcode, source.value, raw)
        else:
            self.barcode_scanned.emit(barcode, source.value)
        self.status_changed.emit()

    # ------------------------------------------------------------------ worker callbacks
    def _on_serial_status(self, connected: bool, message: str) -> None:
        previous = self.state
        self._set_state("CONNECTED" if connected else "DISCONNECTED", message)
        if connected and previous != "CONNECTED":
            self._events.log(Severity.INFO, "SCANNER", "SCANNER_CONNECTED", message)
        elif not connected and previous == "CONNECTED":
            self._events.log(Severity.WARNING, "SCANNER", "SCANNER_DISCONNECTED", message)

    def _on_config_changed(self, section: str) -> None:
        if section != "scanner":
            return
        old_mode = self.mode
        self._cfg = self._config_service.scanner()
        self._normalizer = BarcodeNormalizer(self._cfg)
        if self.mode != old_mode or (self.mode == "SERIAL" and self._worker is None):
            self.stop()
            self.start()
        elif self.mode == "SERIAL":
            self._sig_apply.emit(copy.deepcopy(self._cfg))
            self._set_state("CONNECTING", f"Re-opening {self._cfg.serial_port}…")
        else:
            self.start()
