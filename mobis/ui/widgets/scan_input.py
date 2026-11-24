"""Barcode scan input field for keyboard-wedge (HID) scanners.

Measures the inter-key timing so that scanner bursts can be distinguished from
manual typing, and submits on the configured terminator (Enter, Tab or an idle
timeout for scanners that send no terminator).
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QLineEdit


class ScanInputField(QLineEdit):
    submitted = Signal(str, float, int)  # text, average inter-key interval (ms), key count

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ScanInput")
        self.setPlaceholderText("Scan barcode…")
        self.setClearButtonEnabled(False)
        self._times: list[float] = []
        self._terminator = "ENTER"
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(150)
        self._idle_timer.timeout.connect(self._submit)

    def configure(self, terminator: str, idle_ms: int) -> None:
        self._terminator = terminator
        self._idle_timer.setInterval(max(30, int(idle_ms)))

    def event(self, event):  # Tab must reach keyPressEvent instead of moving focus
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
            and self._terminator == "TAB"
        ):
            self._submit()
            return True
        return super().event(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._submit()
            return
        text = event.text()
        if text and text.isprintable():
            self._times.append(time.perf_counter())
        super().keyPressEvent(event)
        if self._terminator == "NONE" and self.text():
            self._idle_timer.start()

    def _submit(self) -> None:
        self._idle_timer.stop()
        text = self.text()
        times, self._times = self._times, []
        self.clear()
        if not text.strip():
            return
        count = len(times)
        average = ((times[-1] - times[0]) / (count - 1) * 1000.0) if count >= 2 else 9999.0
        self.submitted.emit(text, average, count)
