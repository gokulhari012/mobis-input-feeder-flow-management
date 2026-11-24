"""Serial / virtual COM port barcode scanner reader (lives in its own QThread)."""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from mobis.core.config_models import ScannerConfig

log = logging.getLogger("mobis.scanner")

try:  # pyserial is optional at import time so HID-only installations still start
    import serial  # type: ignore
except ImportError:  # pragma: no cover
    serial = None

_STOP_BITS = {"1": 1, "1.5": 1.5, "2": 2}
MAX_BUFFER = 4096


def split_frames(buffer: bytearray, terminators: list[bytes]) -> list[bytes]:
    """Remove and return complete frames from ``buffer`` (shortest match first, longest terminator wins)."""
    frames = []
    if not terminators:
        return frames
    while True:
        hits = [(buffer.find(t), t) for t in terminators]
        hits = [(i, t) for i, t in hits if i >= 0]
        if not hits:
            break
        index, terminator = min(hits, key=lambda h: (h[0], -len(h[1])))
        frame = bytes(buffer[:index])
        del buffer[: index + len(terminator)]
        if frame:
            frames.append(frame)
    return frames


def terminators_for(config: ScannerConfig) -> list[bytes]:
    mode = config.serial_terminator
    if mode == "CR":
        return [b"\r"]
    if mode == "LF":
        return [b"\n"]
    if mode == "CRLF":
        return [b"\r\n"]
    if mode == "CR_OR_LF":
        return [b"\r\n", b"\r", b"\n"]
    if mode == "CUSTOM":
        try:
            custom = bytes.fromhex(config.custom_terminator_hex.replace(" ", ""))
            return [custom] if custom else [b"\r"]
        except ValueError:
            return [b"\r"]
    return []  # NONE: idle timeout


class SerialScannerWorker(QObject):
    data_received = Signal(str)
    status_changed = Signal(bool, str)

    def __init__(self, config: ScannerConfig):
        super().__init__()
        self._cfg = config
        self._port = None
        self._buffer = bytearray()
        self._last_rx = 0.0
        self._next_open = 0.0
        self._timer: Optional[QTimer] = None
        self._last_status: Optional[tuple[bool, str]] = None

    def _status(self, connected: bool, message: str) -> None:
        if (connected, message) != self._last_status:
            self._last_status = (connected, message)
            self.status_changed.emit(connected, message)

    @Slot()
    def start(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._close()

    @Slot(object)
    def apply_config(self, config: ScannerConfig) -> None:
        self._cfg = config
        self._close()
        self._next_open = 0.0

    def _close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001
                pass

    def _open(self, now: float) -> None:
        cfg = self._cfg
        if serial is None:
            self._status(False, "pyserial is not installed")
            self._next_open = now + 30
            return
        if not cfg.serial_port.strip():
            self._status(False, "No COM port configured")
            self._next_open = now + 5
            return
        try:
            self._port = serial.Serial(
                port=cfg.serial_port.strip(),
                baudrate=cfg.baud_rate,
                bytesize=cfg.data_bits,
                parity=cfg.parity,
                stopbits=_STOP_BITS.get(cfg.stop_bits, 1),
                timeout=0,
            )
        except (ValueError, OSError, Exception) as exc:  # noqa: BLE001 - SerialException derives from IOError
            self._port = None
            self._status(False, f"Cannot open {cfg.serial_port}: {exc}")
            self._next_open = now + max(1.0, cfg.serial_reconnect_s)
            return
        self._buffer.clear()
        self._status(True, f"{cfg.serial_port} open ({cfg.baud_rate} baud, {cfg.data_bits}{cfg.parity}{cfg.stop_bits})")
        log.info("Serial scanner port %s opened", cfg.serial_port)

    def _poll(self) -> None:
        now = time.monotonic()
        if self._port is None:
            if now >= self._next_open:
                self._open(now)
            return
        try:
            waiting = self._port.in_waiting
            if waiting:
                self._buffer.extend(self._port.read(waiting))
                self._last_rx = now
                for frame in split_frames(self._buffer, terminators_for(self._cfg)):
                    self.data_received.emit(frame.decode(self._cfg.encoding or "utf-8", errors="replace"))
                if len(self._buffer) > MAX_BUFFER:
                    self._buffer.clear()
            elif (
                self._buffer
                and not terminators_for(self._cfg)
                and (now - self._last_rx) * 1000 >= self._cfg.serial_idle_submit_ms
            ):
                frame = bytes(self._buffer)
                self._buffer.clear()
                self.data_received.emit(frame.decode(self._cfg.encoding or "utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001 - port unplugged etc.
            self._close()
            self._status(False, f"Port error: {exc}")
            self._next_open = now + max(1.0, self._cfg.serial_reconnect_s)
