"""Built-in PLC simulator (no hardware required).

``SimulatedSlmpClient`` is a drop-in replacement for :class:`SlmpClient`
backed by an in-memory device table.  ``PlcSimulator`` optionally emulates the
PLC side of the loading handshake (shutter open -> light curtain -> shutter
close -> loading complete) so the whole workflow can be tested on a desk.
"""

from __future__ import annotations

import threading
from typing import Optional

from mobis.core.config_models import PlcConfig

from .slmp_protocol import PlcSeries, SlmpAddressError, SlmpConnectionError, parse_device


class SimulatedPlcMemory:
    """Thread-safe device memory shared by the PLC worker and the simulator panel."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, int] = {}
        self._defaults: dict[str, int] = {}

    def read(self, key: str) -> int:
        with self._lock:
            return self._values.get(key, self._defaults.get(key, 0))

    def write(self, key: str, value: int) -> None:
        with self._lock:
            self._values[key] = int(value) & 0xFFFF

    def set_default(self, key: str, value: int) -> None:
        with self._lock:
            self._defaults[key] = int(value) & 0xFFFF

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            merged = dict(self._defaults)
            merged.update(self._values)
            return merged

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


class SimulatedSlmpClient:
    def __init__(self, memory: SimulatedPlcMemory, series: PlcSeries | str = PlcSeries.Q_L):
        self.memory = memory
        self.series = PlcSeries(series)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def _check(self) -> None:
        if not self._connected:
            raise SlmpConnectionError("Simulator not connected")

    def read_value(self, address: str) -> int:
        self._check()
        addr = parse_device(address, self.series)
        value = self.memory.read(str(addr))
        return (1 if value else 0) if addr.is_bit else value

    def write_value(self, address: str, value: int) -> None:
        self._check()
        addr = parse_device(address, self.series)
        self.memory.write(str(addr), (1 if int(value) else 0) if addr.is_bit else int(value) & 0xFFFF)

    def read_type_name(self) -> tuple[str, int]:
        self._check()
        return "SIMULATOR", 0


class PlcSimulator:
    """Emulates the PLC loading sequence once authorisation is received."""

    def __init__(self, memory: SimulatedPlcMemory):
        self.memory = memory
        self.phase = "IDLE"
        self._due = 0.0

    @staticmethod
    def _key(address: str, series: PlcSeries) -> Optional[str]:
        if not (address or "").strip():
            return None
        try:
            return str(parse_device(address, series))
        except SlmpAddressError:
            return None

    def _authorized(self, cfg: PlcConfig, series: PlcSeries) -> bool:
        if cfg.validation_result_enabled:
            key = self._key(cfg.validation_result_address, series)
            if key:
                return self.memory.read(key) == (cfg.validation_ok_value & 0xFFFF)
        key = self._key(cfg.hopper_code_address, series)
        return bool(key and self.memory.read(key) != 0)

    def _set(self, address: str, value: int, series: PlcSeries) -> None:
        key = self._key(address, series)
        if key:
            self.memory.write(key, value)

    def tick(self, cfg: PlcConfig, now: float) -> None:
        if not cfg.sim_auto_loading:
            return
        series = PlcSeries(cfg.plc_series)
        delay = max(0.2, cfg.sim_step_delay_s)
        closed = 0 if cfg.shutter_open_value else 1
        curtain_off = 0 if cfg.light_curtain_active_value else 1
        not_complete = 0 if cfg.loading_complete_value else 1
        if not self._authorized(cfg, series):
            if self.phase != "IDLE":
                self._set(cfg.shutter_status_address, closed, series)
                self._set(cfg.light_curtain_address, curtain_off, series)
                self._set(cfg.loading_complete_address, not_complete, series)
                self.phase = "IDLE"
            return
        if self.phase == "IDLE":
            self.phase, self._due = "WAIT_OPEN", now + delay
        elif now < self._due:
            return
        elif self.phase == "WAIT_OPEN":
            self._set(cfg.shutter_status_address, cfg.shutter_open_value, series)
            self.phase, self._due = "OPEN", now + delay
        elif self.phase == "OPEN":
            self._set(cfg.light_curtain_address, cfg.light_curtain_active_value, series)
            self.phase, self._due = "FILLING", now + delay
        elif self.phase == "FILLING":
            self._set(cfg.light_curtain_address, curtain_off, series)
            self._set(cfg.shutter_status_address, closed, series)
            self.phase, self._due = "CLOSED", now + delay / 2
        elif self.phase == "CLOSED":
            self._set(cfg.loading_complete_address, cfg.loading_complete_value, series)
            self.phase = "DONE"
