"""PLC communication worker (lives in a dedicated QThread).

Responsibilities: connection management with automatic reconnection,
heartbeat, optional PLC watchdog supervision, cyclic polling of PLC -> PC
signals and execution of write requests.

Outputs (alarm, alarm code, validation result, hopper / destination codes)
are kept as a *desired state*: after every (re)connection the complete desired
state is written again, so a communication interruption can never leave the
PLC with a stale alarm or authorisation value.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from mobis.core.config_models import PlcConfig
from mobis.hardware.mitsubishi_slmp import SlmpClient
from mobis.hardware.plc_simulator import PlcSimulator, SimulatedPlcMemory, SimulatedSlmpClient
from mobis.hardware.slmp_protocol import (
    FrameConfig,
    PlcSeries,
    SlmpAddressError,
    SlmpConnectionError,
    SlmpEndCodeError,
    SlmpError,
    normalize_address,
)

log = logging.getLogger("mobis.plc")

OUTPUT_KEYS = ("alarm", "alarm_code", "validation", "hopper_code", "destination_code")
_REJECTED = (SlmpEndCodeError, SlmpAddressError)  # PLC answered / bad address: link is still healthy
_LINK_ERRORS = (SlmpError, OSError)


class PlcWorker(QObject):
    connection_changed = Signal(bool, str)
    heartbeat_changed = Signal(str, str)              # state, detail
    signals_updated = Signal(object, float)           # {address: value|None}, wall time
    operation_finished = Signal(int, bool, object, str)
    stats_changed = Signal(object)
    comm_event = Signal(str, str, str)                # severity, event type, message

    def __init__(self, config: PlcConfig, memory: SimulatedPlcMemory, force_simulation: bool = False):
        super().__init__()
        self._cfg = config
        self._memory = memory
        self._force_sim = force_simulation
        self._simulator = PlcSimulator(memory)
        self._client = None
        self._connected = False
        self._timer: Optional[QTimer] = None
        self._stopping = False
        self._desired: dict[str, int] = {}
        self._watch: list[str] = []
        self._next_connect = 0.0
        self._next_heartbeat = 0.0
        self._next_poll = 0.0
        self._hb_value = 0
        self._hb_state: tuple[str, str] = ("", "")
        self._watchdog_last: Optional[int] = None
        self._watchdog_changed_at = 0.0
        self._last_error = ""
        self._address_errors: dict[str, str] = {}
        self._stats = {
            "connected": False, "simulated": False, "connected_since": None, "connects": 0,
            "connect_failures": 0, "comm_errors": 0, "last_error": "", "cpu_model": "",
            "last_poll_ms": 0.0, "heartbeat_value": 0,
        }
        self._reset_desired()

    # ------------------------------------------------------------------ helpers
    @property
    def simulated(self) -> bool:
        return self._force_sim or self._cfg.simulation_mode

    def _series(self) -> PlcSeries:
        try:
            return PlcSeries(self._cfg.plc_series)
        except ValueError:
            return PlcSeries.Q_L

    def _reset_desired(self) -> None:
        c = self._cfg
        self._desired = {
            "alarm": c.alarm_off_value,
            "alarm_code": 0,
            "validation": c.validation_idle_value,
            "hopper_code": 0,
            "destination_code": 0,
        }

    def _output_address(self, key: str) -> str:
        c = self._cfg
        return {
            "alarm": c.alarm_address,
            "alarm_code": c.alarm_code_address,
            "validation": c.validation_result_address if c.validation_result_enabled else "",
            "hopper_code": c.hopper_code_address,
            "destination_code": c.destination_code_address,
        }.get(key, "").strip()

    def _emit_stats(self) -> None:
        self._stats["connected"] = self._connected
        self._stats["simulated"] = self.simulated
        self.stats_changed.emit(dict(self._stats))

    def _set_heartbeat(self, state: str, detail: str) -> None:
        if (state, detail) != self._hb_state:
            self._hb_state = (state, detail)
            self.heartbeat_changed.emit(state, detail)

    # ------------------------------------------------------------------ lifecycle
    @Slot()
    def start(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        log.info("PLC worker started (simulation=%s)", self.simulated)

    @Slot()
    def stop(self) -> None:
        self._stopping = True
        if self._timer is not None:
            self._timer.stop()
        if self._connected and self._client is not None:
            c = self._cfg
            for key, value in (("validation", c.validation_idle_value), ("hopper_code", 0), ("destination_code", 0)):
                address = self._output_address(key)
                if not address:
                    continue
                try:
                    self._client.write_value(address, value)
                except Exception as exc:  # noqa: BLE001 - best effort on shutdown
                    log.warning("Could not revoke %s on shutdown: %s", key, exc)
                    break
        self._close_client()
        log.info("PLC worker stopped")

    def _close_client(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ cycle
    def _tick(self) -> None:
        if self._stopping:
            return
        now = time.monotonic()
        cfg = self._cfg
        if not cfg.enabled:
            if self._client is not None:
                self._close_client()
                self.connection_changed.emit(False, "PLC communication disabled")
                self._emit_stats()
            self._set_heartbeat("DISABLED", "PLC communication disabled")
            return
        if not self._connected:
            if now >= self._next_connect:
                self._try_connect(now)
            return
        if self.simulated:
            self._simulator.tick(cfg, now)
        if now >= self._next_heartbeat:
            self._next_heartbeat = now + max(0.1, cfg.heartbeat_interval_ms / 1000.0)
            self._do_heartbeat(now)
            if not self._connected:
                return
        if now >= self._next_poll:
            self._next_poll = now + max(0.05, cfg.poll_interval_ms / 1000.0)
            self._do_poll()

    def _create_client(self):
        cfg = self._cfg
        series = self._series()
        if self.simulated:
            return SimulatedSlmpClient(self._memory, series)
        if not cfg.host.strip() or not 1 <= cfg.port <= 65535:
            raise SlmpConnectionError("PLC IP address / port not configured")
        frame = FrameConfig(
            frame_type=cfg.frame_type,
            data_code=cfg.data_code,
            series=series,
            network_no=cfg.network_no & 0xFF,
            pc_no=cfg.pc_no & 0xFF,
            module_io_no=cfg.module_io_no & 0xFFFF,
            station_no=cfg.station_no & 0xFF,
            monitoring_timer=max(1, cfg.monitoring_timer) & 0xFFFF,
        )
        return SlmpClient(cfg.host.strip(), cfg.port, frame, cfg.transport, cfg.connect_timeout_s, cfg.io_timeout_s)

    def _try_connect(self, now: float) -> None:
        cfg = self._cfg
        self._next_connect = now + max(1.0, cfg.reconnect_interval_s)
        try:
            client = self._create_client()
            client.connect()
        except (SlmpError, OSError, ValueError) as exc:
            self._stats["connect_failures"] += 1
            message = str(exc)
            self._stats["last_error"] = message
            if message != self._last_error:
                self._last_error = message
                self.comm_event.emit("ERROR", "PLC_CONNECT_FAILED", message)
            self.connection_changed.emit(False, message)
            self._set_heartbeat("DISCONNECTED", message)
            self._emit_stats()
            return
        self._client = client
        self._connected = True
        self._stats["connects"] += 1
        self._stats["connected_since"] = time.time()
        try:
            name, code = client.read_type_name()
            self._stats["cpu_model"] = name + (f" (0x{code:04X})" if code else "")
        except SlmpEndCodeError:
            self._stats["cpu_model"] = "(type name not supported)"
        except _LINK_ERRORS as exc:
            self._handle_comm_error(exc)
            return
        if not self._sync_outputs():
            return
        self._last_error = ""
        self._watchdog_last = None
        self._watchdog_changed_at = now
        label = "built-in simulator" if self.simulated else f"{cfg.host}:{cfg.port}"
        self.connection_changed.emit(True, f"Connected to {label}")
        self.comm_event.emit("INFO", "PLC_CONNECTED", f"PLC connected ({label}, {self._stats['cpu_model']})")
        self._next_heartbeat = now
        self._next_poll = now
        self._emit_stats()

    def _handle_comm_error(self, exc: BaseException) -> None:
        message = str(exc) or exc.__class__.__name__
        log.warning("PLC communication error: %s", message)
        self._stats["comm_errors"] += 1
        self._stats["last_error"] = message
        self.comm_event.emit("ERROR", "PLC_COMM_ERROR", message)
        self._close_client()
        self._last_error = message
        self._next_connect = time.monotonic() + max(1.0, self._cfg.reconnect_interval_s)
        self.connection_changed.emit(False, message)
        self._set_heartbeat("DISCONNECTED", message)
        self._emit_stats()

    # ------------------------------------------------------------------ outputs
    def _write_output(self, key: str) -> None:
        address = self._output_address(key)
        if address and key in self._desired:
            self._client.write_value(address, self._desired[key])

    def _write_output_safe(self, key: str) -> bool:
        try:
            self._write_output(key)
            return True
        except _REJECTED as exc:
            self.comm_event.emit("ERROR", "PLC_WRITE_REJECTED", f"Writing {key} to {self._output_address(key)}: {exc}")
            return True
        except _LINK_ERRORS as exc:
            self._handle_comm_error(exc)
            return False

    def _sync_outputs(self) -> bool:
        """Write the complete desired output state (after (re)connection)."""
        for key in OUTPUT_KEYS:
            if not self._write_output_safe(key):
                return False
        return True

    # ------------------------------------------------------------------ heartbeat / poll
    def _do_heartbeat(self, now: float) -> None:
        cfg = self._cfg
        address = cfg.heartbeat_address.strip()
        try:
            if address:
                if cfg.heartbeat_mode == "COUNTER":
                    self._hb_value = (self._hb_value + 1) % 32768
                else:
                    self._hb_value = 1 - (self._hb_value & 1)
                self._client.write_value(address, self._hb_value)
                self._stats["heartbeat_value"] = self._hb_value
            elif not self.simulated:
                try:
                    self._client.read_type_name()  # link check when no heartbeat device is configured
                except SlmpEndCodeError:
                    pass
        except _REJECTED as exc:
            self._set_heartbeat("FAIL", f"Heartbeat write to {address} rejected: {exc}")
            return
        except _LINK_ERRORS as exc:
            self._handle_comm_error(exc)
            return

        watchdog = cfg.plc_watchdog_address.strip()
        if watchdog:
            try:
                value = self._client.read_value(watchdog)
            except _REJECTED as exc:
                self._set_heartbeat("FAIL", f"PLC watchdog {watchdog} read failed: {exc}")
                return
            except _LINK_ERRORS as exc:
                self._handle_comm_error(exc)
                return
            if value != self._watchdog_last:
                self._watchdog_last = value
                self._watchdog_changed_at = now
            elif now - self._watchdog_changed_at > max(1.0, cfg.plc_watchdog_timeout_s):
                self._set_heartbeat(
                    "PLC_STALE",
                    f"PLC watchdog {watchdog} unchanged for {now - self._watchdog_changed_at:.0f} s "
                    "(PLC program stopped?)",
                )
                return
        if address:
            self._set_heartbeat("OK", f"Heartbeat written to {address} every {cfg.heartbeat_interval_ms} ms")
        else:
            self._set_heartbeat("NOT_CONFIGURED", "Heartbeat address not configured (link check only)")

    def _poll_addresses(self) -> list[str]:
        series = self._series()
        result: list[str] = []
        for address in self._cfg.input_addresses() + self._watch:
            key = normalize_address(address, series)
            if key and key not in result:
                result.append(key)
        return result

    def _do_poll(self) -> None:
        values: dict[str, Optional[int]] = {}
        started = time.perf_counter()
        for address in self._poll_addresses():
            try:
                values[address] = self._client.read_value(address)
                self._address_errors.pop(address, None)
            except _REJECTED as exc:
                values[address] = None
                if self._address_errors.get(address) != str(exc):
                    self._address_errors[address] = str(exc)
                    self.comm_event.emit("WARNING", "PLC_READ_REJECTED", f"Reading {address} failed: {exc}")
            except _LINK_ERRORS as exc:
                self._handle_comm_error(exc)
                return
        self._stats["last_poll_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        self.signals_updated.emit(values, time.time())

    # ------------------------------------------------------------------ requests (queued from the GUI)
    @Slot(object)
    def apply_config(self, config: PlcConfig) -> None:
        old = self._cfg
        self._cfg = config
        if old.connection_signature() != config.connection_signature():
            if self._client is not None:
                self._close_client()
                self.connection_changed.emit(False, "Configuration changed - reconnecting")
            self._next_connect = 0.0
            self._last_error = ""
        elif self._connected:
            self._sync_outputs()
        self._next_heartbeat = 0.0
        self._emit_stats()

    @Slot(object)
    def set_watch(self, addresses) -> None:
        self._watch = [a for a in addresses if a]

    @Slot(str, int)
    def set_output(self, key: str, value: int) -> None:
        self._desired[key] = int(value)
        if self._connected and self._client is not None:
            self._write_output_safe(key)

    @Slot(int, object, bool)
    def write_outputs(self, request_id: int, items, revert_on_failure: bool) -> None:
        previous = {key: self._desired.get(key, 0) for key, _value in items}
        for key, value in items:
            self._desired[key] = int(value)
        if not self._connected or self._client is None:
            if revert_on_failure:
                self._desired.update(previous)
            self.operation_finished.emit(request_id, False, None, "PLC not connected")
            return
        try:
            for key, _value in items:
                self._write_output(key)
        except _REJECTED as exc:
            if revert_on_failure:
                self._desired.update(previous)
                for key, _value in items:
                    self._write_output_safe(key)
            self.operation_finished.emit(request_id, False, None, str(exc))
            return
        except _LINK_ERRORS as exc:
            if revert_on_failure:
                self._desired.update(previous)
            self._handle_comm_error(exc)
            self.operation_finished.emit(request_id, False, None, str(exc))
            return
        self.operation_finished.emit(request_id, True, None, "")

    @Slot()
    def pulse_alarm_reset(self) -> None:
        cfg = self._cfg
        address = cfg.alarm_reset_command_address.strip()
        if not address or not self._connected or self._client is None:
            return
        try:
            self._client.write_value(address, cfg.alarm_reset_command_value)
        except _REJECTED as exc:
            self.comm_event.emit("ERROR", "PLC_WRITE_REJECTED", f"Alarm reset command {address}: {exc}")
            return
        except _LINK_ERRORS as exc:
            self._handle_comm_error(exc)
            return
        QTimer.singleShot(max(50, cfg.alarm_reset_pulse_ms), lambda: self._release_pulse(address))

    def _release_pulse(self, address: str) -> None:
        if self._connected and self._client is not None:
            try:
                self._client.write_value(address, 0)
            except _REJECTED as exc:
                self.comm_event.emit("ERROR", "PLC_WRITE_REJECTED", f"Alarm reset release {address}: {exc}")
            except _LINK_ERRORS as exc:
                self._handle_comm_error(exc)

    @Slot(int, str)
    def read_device(self, request_id: int, address: str) -> None:
        if not self._connected or self._client is None:
            self.operation_finished.emit(request_id, False, None, "PLC not connected")
            return
        try:
            value = self._client.read_value(address)
        except _REJECTED as exc:
            self.operation_finished.emit(request_id, False, None, str(exc))
            return
        except _LINK_ERRORS as exc:
            self._handle_comm_error(exc)
            self.operation_finished.emit(request_id, False, None, str(exc))
            return
        self.operation_finished.emit(request_id, True, value, "")

    @Slot(int, str, int)
    def write_device(self, request_id: int, address: str, value: int) -> None:
        if not self._connected or self._client is None:
            self.operation_finished.emit(request_id, False, None, "PLC not connected")
            return
        try:
            self._client.write_value(address, value)
        except _REJECTED as exc:
            self.operation_finished.emit(request_id, False, None, str(exc))
            return
        except _LINK_ERRORS as exc:
            self._handle_comm_error(exc)
            self.operation_finished.emit(request_id, False, None, str(exc))
            return
        self.comm_event.emit("INFO", "PLC_MANUAL_WRITE", f"Manual write {address} = {value}")
        self.operation_finished.emit(request_id, True, value, "")

    @Slot(int)
    def test_connection(self, request_id: int) -> None:
        if not self._connected:
            self._try_connect(time.monotonic())
        if not self._connected or self._client is None:
            self.operation_finished.emit(request_id, False, None, self._last_error or "Connection failed")
            return
        try:
            name, code = self._client.read_type_name()
            info = f"Connected. CPU type: {name}" + (f" (code 0x{code:04X})" if code else "")
        except SlmpEndCodeError as exc:
            info = f"Connected (PLC answered, type name not available: {exc})"
        except _LINK_ERRORS as exc:
            self._handle_comm_error(exc)
            self.operation_finished.emit(request_id, False, None, str(exc))
            return
        self.operation_finished.emit(request_id, True, info, "")
