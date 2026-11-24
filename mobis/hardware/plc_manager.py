"""PLC facade used by the GUI thread.

Owns the :class:`~mobis.workers.plc_worker.PlcWorker` thread, keeps the latest
status and a timestamped cache of PLC input signals, and translates logical
requests (alarm on/off, authorise loading, revoke) into device writes using the
configured addresses and values.  All methods are non-blocking.
"""

from __future__ import annotations

import copy
import ipaddress
import itertools
import logging
import re
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

from mobis.core.config_models import PlcConfig
from mobis.core.models import Severity
from mobis.workers.plc_worker import PlcWorker

from .plc_simulator import SimulatedPlcMemory
from .slmp_protocol import PlcSeries, SlmpAddressError, normalize_address, parse_device

log = logging.getLogger("mobis.plc")

ADDRESS_FIELDS = {
    "heartbeat_address": "Heartbeat",
    "plc_watchdog_address": "PLC watchdog",
    "alarm_address": "Alarm",
    "alarm_code_address": "Alarm code",
    "alarm_ack_address": "Alarm acknowledgement",
    "alarm_reset_command_address": "Alarm reset command",
    "validation_result_address": "Validation result",
    "hopper_code_address": "Hopper code",
    "destination_code_address": "Destination code",
    "loading_complete_address": "Loading complete",
    "shutter_status_address": "Shutter status",
    "light_curtain_address": "Light curtain",
}
OUTPUT_FIELDS = (
    "heartbeat_address", "alarm_address", "alarm_code_address", "alarm_reset_command_address",
    "validation_result_address", "hopper_code_address", "destination_code_address",
)
HEARTBEAT_LABELS = {
    "OK": ("OK", "ok"),
    "NOT_CONFIGURED": ("Not configured", "warn"),
    "FAIL": ("Failed", "ng"),
    "PLC_STALE": ("PLC watchdog stale", "ng"),
    "DISCONNECTED": ("Disconnected", "ng"),
    "DISABLED": ("Disabled", "neutral"),
}


def heartbeat_label(state: str) -> tuple[str, str]:
    """Display text and level of a heartbeat state."""
    return HEARTBEAT_LABELS.get(state, (state.replace("_", " ").title(), "neutral"))


_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def address_problem(address: str, series: str) -> str:
    try:
        parse_device(address, series)
        return ""
    except SlmpAddressError as exc:
        return str(exc)


def validate_plc_config(cfg: PlcConfig) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for a PLC configuration."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        series = PlcSeries(cfg.plc_series).value
    except ValueError:
        errors.append(f"Unknown PLC series '{cfg.plc_series}'.")
        series = PlcSeries.Q_L.value
    used: dict[str, str] = {}
    for field_name, label in ADDRESS_FIELDS.items():
        value = getattr(cfg, field_name).strip()
        if not value:
            continue
        problem = address_problem(value, series)
        if problem:
            errors.append(f"{label} address: {problem}")
            continue
        key = normalize_address(value, series)
        if field_name in OUTPUT_FIELDS:
            if key in used:
                errors.append(f"{label} address {key} is already used for {used[key]}.")
            used[key] = label

    if cfg.enabled and not cfg.simulation_mode:
        host = cfg.host.strip()
        if not host:
            errors.append("PLC IP address is required.")
        else:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not _HOSTNAME.match(host):
                    errors.append(f"'{host}' is not a valid IP address or host name.")
        if not 1 <= cfg.port <= 65535:
            errors.append("PLC port must be between 1 and 65535.")
        if not cfg.heartbeat_address.strip():
            warnings.append("No heartbeat address: the PLC cannot detect a failure of this PC.")
        if not cfg.alarm_address.strip():
            warnings.append("No alarm address: mismatches will not raise the PLC alarm.")
        if not cfg.authorization_outputs():
            warnings.append("No authorisation output (validation result / hopper code) configured: "
                            "loading cannot be authorised.")
    if cfg.transport not in ("TCP", "UDP"):
        errors.append("Transport must be TCP or UDP.")
    if cfg.alarm_reset_mode in ("PLC_ACK", "OPERATOR_OR_PLC") and not cfg.alarm_ack_address.strip():
        errors.append("The selected alarm reset mode requires an alarm acknowledgement address.")
    if cfg.loading_confirmation_mode == "PLC_SIGNAL" and not cfg.loading_complete_address.strip():
        errors.append("Loading confirmation 'PLC signal' requires the loading-complete address.")
    if cfg.loading_confirmation_mode == "SHUTTER_CYCLE" and not cfg.shutter_status_address.strip():
        errors.append("Loading confirmation 'Shutter cycle' requires the shutter status address.")
    if cfg.validation_result_enabled and not cfg.validation_result_address.strip():
        errors.append("Validation result output is enabled but no address is configured.")
    if len({cfg.validation_ok_value, cfg.validation_ng_value, cfg.validation_idle_value}) < 3:
        errors.append("Validation OK, NG and idle values must all be different.")
    if cfg.alarm_on_value == cfg.alarm_off_value:
        errors.append("Alarm ON and OFF values must be different.")
    if cfg.poll_interval_ms < 50:
        errors.append("Poll interval must be at least 50 ms.")
    if cfg.heartbeat_interval_ms < 100:
        errors.append("Heartbeat interval must be at least 100 ms.")
    return errors, warnings


class PlcManager(QObject):
    connection_changed = Signal(bool, str)
    heartbeat_changed = Signal(str, str)
    signals_updated = Signal(object)
    alarm_acknowledged = Signal()
    status_changed = Signal()

    _sig_set_output = Signal(str, int)
    _sig_write_outputs = Signal(int, object, bool)
    _sig_read = Signal(int, str)
    _sig_write = Signal(int, str, int)
    _sig_apply_config = Signal(object)
    _sig_set_watch = Signal(object)
    _sig_pulse_reset = Signal()
    _sig_test = Signal(int)
    _sig_stop = Signal()

    def __init__(self, config_service, events, force_simulation: bool = False):
        super().__init__()
        self._config_service = config_service
        self._events = events
        self._force_sim = force_simulation
        self._cfg: PlcConfig = config_service.plc()
        self.sim_memory = SimulatedPlcMemory()
        self.connected = False
        self.status_text = "Not started"
        self.heartbeat_state = "DISCONNECTED"
        self.heartbeat_detail = ""
        self.stats: dict = {}
        self.alarm_active = False
        self.alarm_code = 0
        self.authorized: Optional[tuple[int, int]] = None
        self._cache: dict[str, tuple[Optional[int], float]] = {}
        self._callbacks: dict[int, Callable] = {}
        self._ids = itertools.count(1)
        self._ack_active = False
        self._equipment_signals: list[tuple[str, int]] = []
        self._thread: Optional[QThread] = None
        self._worker: Optional[PlcWorker] = None
        config_service.changed.connect(self._on_config_changed)

    # ------------------------------------------------------------------ properties
    @property
    def config(self) -> PlcConfig:
        return copy.deepcopy(self._cfg)

    @property
    def simulated(self) -> bool:
        return self._force_sim or self._cfg.simulation_mode

    @property
    def forced_simulation(self) -> bool:
        return self._force_sim

    @property
    def series(self) -> str:
        return self._cfg.plc_series

    def normalize_address(self, address: str) -> str:
        return normalize_address(address, self.series)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread()
        self._thread.setObjectName("PLC-Worker")
        worker = PlcWorker(copy.deepcopy(self._cfg), self.sim_memory, self._force_sim)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.start)
        worker.connection_changed.connect(self._on_worker_connection)
        worker.heartbeat_changed.connect(self._on_heartbeat)
        worker.signals_updated.connect(self._on_signals)
        worker.operation_finished.connect(self._on_operation_finished)
        worker.stats_changed.connect(self._on_stats)
        worker.comm_event.connect(self._on_comm_event)
        self._sig_set_output.connect(worker.set_output)
        self._sig_write_outputs.connect(worker.write_outputs)
        self._sig_read.connect(worker.read_device)
        self._sig_write.connect(worker.write_device)
        self._sig_apply_config.connect(worker.apply_config)
        self._sig_set_watch.connect(worker.set_watch)
        self._sig_pulse_reset.connect(worker.pulse_alarm_reset)
        self._sig_test.connect(worker.test_connection)
        self._sig_stop.connect(worker.stop, Qt.ConnectionType.BlockingQueuedConnection)
        self._worker = worker
        self._thread.start()
        self._push_watch()

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread.isRunning():
            self._sig_stop.emit()  # blocking: revokes authorisation outputs and closes the socket
        self._thread.quit()
        self._thread.wait(5000)
        self._thread = None
        self._worker = None

    @property
    def worker_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    # ------------------------------------------------------------------ status / signals
    def is_ready(self) -> tuple[bool, str]:
        c = self._cfg
        if not c.enabled:
            return False, "PLC communication is disabled"
        if not self.connected:
            return False, f"PLC not connected ({self.status_text})"
        if self.heartbeat_state in ("FAIL", "PLC_STALE", "DISCONNECTED", "DISABLED"):
            return False, f"PLC heartbeat {self.heartbeat_state.replace('_', ' ').lower()}: {self.heartbeat_detail}"
        if not self.simulated and not c.authorization_outputs():
            return False, "No PLC authorisation output configured (validation result / hopper code)"
        return True, "Connected (simulation)" if self.simulated else "Connected"

    def get_signal(self, address: str, max_age_s: Optional[float] = None) -> Optional[int]:
        entry = self._cache.get(self.normalize_address(address))
        if entry is None:
            return None
        value, received = entry
        limit = self._cfg.signal_max_age_s if max_age_s is None else max_age_s
        if time.monotonic() - received > max(0.5, limit):
            return None
        return value

    def signal_values(self) -> dict[str, Optional[int]]:
        limit = max(0.5, self._cfg.signal_max_age_s)
        now = time.monotonic()
        return {k: v for k, (v, t) in self._cache.items() if now - t <= limit}

    def cache_snapshot(self) -> dict[str, tuple[Optional[int], float]]:
        now = time.monotonic()
        return {k: (v, now - t) for k, (v, t) in self._cache.items()}

    def set_equipment_signals(self, signals: list[tuple[str, int]]) -> None:
        """Equipment limit switch / ready signals that must be polled continuously."""
        self._equipment_signals = [(a.strip(), int(v)) for a, v in signals if a and a.strip()]
        self._push_watch()

    def _push_watch(self) -> None:
        addresses = [self.normalize_address(a) for a, _v in self._equipment_signals]
        if self.simulated:
            for address, ok_value in self._equipment_signals:
                self.sim_memory.set_default(self.normalize_address(address), ok_value)
        self._sig_set_watch.emit(addresses)

    # ------------------------------------------------------------------ logical outputs
    def set_alarm(self, active: bool, reason_code: int = 0) -> None:
        c = self._cfg
        self.alarm_active = active
        self.alarm_code = int(reason_code) if active else 0
        self._sig_set_output.emit("alarm", c.alarm_on_value if active else c.alarm_off_value)
        self._sig_set_output.emit("alarm_code", self.alarm_code)
        if self.authorized is None:
            self._sig_set_output.emit("validation", c.validation_ng_value if active else c.validation_idle_value)
        if not active:
            self._sig_pulse_reset.emit()
        self.status_changed.emit()

    def authorize_loading(self, hopper_code: int, destination_code: int,
                          callback: Callable[[bool, str], None]) -> None:
        c = self._cfg
        items = [("hopper_code", int(hopper_code)), ("destination_code", int(destination_code)),
                 ("validation", c.validation_ok_value)]
        request_id = next(self._ids)

        def done(ok: bool, _value, error: str) -> None:
            if ok:
                self.authorized = (int(hopper_code), int(destination_code))
                self.status_changed.emit()
            callback(ok, error)

        self._callbacks[request_id] = done
        self._sig_write_outputs.emit(request_id, items, True)

    def clear_authorization(self) -> None:
        c = self._cfg
        self.authorized = None
        items = [
            ("validation", c.validation_ng_value if self.alarm_active else c.validation_idle_value),
            ("hopper_code", 0),
            ("destination_code", 0),
        ]
        request_id = next(self._ids)

        def done(ok: bool, _value, error: str) -> None:
            if not ok:
                log.warning("Revoking PLC authorisation failed (re-sent after reconnection): %s", error)

        self._callbacks[request_id] = done
        self._sig_write_outputs.emit(request_id, items, False)
        self.status_changed.emit()

    def _resend_outputs(self) -> None:
        c = self._cfg
        self._sig_set_output.emit("alarm", c.alarm_on_value if self.alarm_active else c.alarm_off_value)
        self._sig_set_output.emit("alarm_code", self.alarm_code)
        if self.authorized is not None:
            hopper, destination = self.authorized
            self._sig_set_output.emit("hopper_code", hopper)
            self._sig_set_output.emit("destination_code", destination)
            self._sig_set_output.emit("validation", c.validation_ok_value)
        else:
            self._sig_set_output.emit("hopper_code", 0)
            self._sig_set_output.emit("destination_code", 0)
            self._sig_set_output.emit(
                "validation", c.validation_ng_value if self.alarm_active else c.validation_idle_value
            )

    # ------------------------------------------------------------------ manual testing
    def read_device(self, address: str, callback: Callable[[bool, object, str], None]) -> None:
        request_id = next(self._ids)
        self._callbacks[request_id] = callback
        self._sig_read.emit(request_id, address)

    def write_device(self, address: str, value: int, callback: Callable[[bool, object, str], None]) -> None:
        request_id = next(self._ids)
        self._callbacks[request_id] = callback
        self._sig_write.emit(request_id, address, int(value))

    def test_connection(self, callback: Callable[[bool, object, str], None]) -> None:
        request_id = next(self._ids)
        self._callbacks[request_id] = callback
        self._sig_test.emit(request_id)

    # ------------------------------------------------------------------ worker callbacks (GUI thread)
    def _on_worker_connection(self, connected: bool, message: str) -> None:
        changed = connected != self.connected
        self.connected = connected
        self.status_text = message
        if not connected:
            self._cache.clear()
        if changed and connected:
            self._resend_outputs()
        self.connection_changed.emit(connected, message)
        self.status_changed.emit()

    def _on_heartbeat(self, state: str, detail: str) -> None:
        self.heartbeat_state = state
        self.heartbeat_detail = detail
        self.heartbeat_changed.emit(state, detail)
        self.status_changed.emit()

    def _on_signals(self, values, _timestamp: float) -> None:
        now = time.monotonic()
        for key, value in values.items():
            self._cache[key] = (value, now)
        ack = self._cfg.alarm_ack_address.strip()
        if ack:
            key = self.normalize_address(ack)
            if key in values:
                value = values[key]
                active = value is not None and value == (self._cfg.alarm_ack_value & 0xFFFF)
                if active and not self._ack_active:
                    self.alarm_acknowledged.emit()
                self._ack_active = active
        self.signals_updated.emit(values)

    def _on_operation_finished(self, request_id: int, ok: bool, value, error: str) -> None:
        callback = self._callbacks.pop(request_id, None)
        if callback is None:
            return
        try:
            callback(ok, value, error)
        except Exception:  # noqa: BLE001
            log.exception("PLC callback failed")

    def _on_stats(self, stats) -> None:
        self.stats = stats
        self.status_changed.emit()

    def _on_comm_event(self, severity: str, event_type: str, message: str) -> None:
        self._events.log(Severity(severity), "PLC", event_type, message)

    def _on_config_changed(self, section: str) -> None:
        if section != "plc":
            return
        self._cfg = self._config_service.plc()
        self._sig_apply_config.emit(copy.deepcopy(self._cfg))
        self._resend_outputs()
        self._push_watch()
        self.status_changed.emit()
