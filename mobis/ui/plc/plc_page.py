"""Mitsubishi PLC (SLMP) configuration, status, manual testing and simulator."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mobis.core.config_models import PlcConfig
from mobis.core.permissions import Permission
from mobis.hardware.plc_manager import address_problem, heartbeat_label, validate_plc_config
from mobis.hardware.slmp_protocol import SUPPORTED_DEVICES_TEXT, to_signed16
from mobis.ui.base_page import BasePage
from mobis.ui.widgets.common import (
    Card,
    KeyValueGrid,
    MessageBox,
    PageHeader,
    button,
    confirm,
    label,
    scroll_wrap,
    show_error,
    show_info,
    show_warning,
)
from mobis.ui.widgets.config_form import ConfigForm, FieldSpec
from mobis.ui.widgets.table_model import Column, RecordTableModel, make_table
from mobis.utils.timeutil import fmt_time

ADDRESS_HINT = f"Device address, e.g. D100, M20, X1F. Supported: {SUPPORTED_DEVICES_TEXT}"

CONNECTION_SECTIONS = [
    ("Mode", [
        FieldSpec("enabled", "PLC communication enabled", "bool"),
        FieldSpec("simulation_mode", "Use built-in PLC simulator", "bool",
                  tooltip="Test without a PLC. Disable on site after configuring the real PLC."),
    ]),
    ("Ethernet", [
        FieldSpec("host", "PLC IP address", placeholder="e.g. 192.168.3.39"),
        FieldSpec("port", "SLMP port", "int", maximum=65535, hint="As configured in the PLC Ethernet parameters"),
        FieldSpec("transport", "Transport", "choice", [("TCP", "TCP"), ("UDP", "UDP")]),
        FieldSpec("connect_timeout_s", "Connection timeout", "float", minimum=0.5, maximum=60, suffix="s"),
        FieldSpec("io_timeout_s", "Response timeout", "float", minimum=0.2, maximum=60, suffix="s"),
        FieldSpec("reconnect_interval_s", "Reconnection interval", "float", minimum=1, maximum=600, suffix="s"),
        FieldSpec("poll_interval_ms", "Signal poll interval", "int", minimum=50, maximum=10000, suffix="ms"),
        FieldSpec("signal_max_age_s", "Signal max age", "float", minimum=0.5, maximum=60, suffix="s",
                  hint="Older PLC input values are treated as unknown"),
    ]),
    ("SLMP Settings", [
        FieldSpec("plc_series", "PLC series", "choice", [("Q_L", "MELSEC Q / L (and compatible)"),
                                                          ("IQ_R", "MELSEC iQ-R"), ("IQ_F", "MELSEC iQ-F (FX5)")]),
        FieldSpec("frame_type", "Frame", "choice", [("3E", "3E frame"), ("4E", "4E frame")]),
        FieldSpec("data_code", "Communication data code", "choice", [("BINARY", "Binary"), ("ASCII", "ASCII")]),
        FieldSpec("network_no", "Network No.", "int", maximum=255),
        FieldSpec("pc_no", "PC No. (station)", "hexint", maximum=255),
        FieldSpec("module_io_no", "Request destination module I/O", "hexint", maximum=65535),
        FieldSpec("station_no", "Request destination station", "int", maximum=255),
        FieldSpec("monitoring_timer", "Monitoring timer (x250 ms)", "int", minimum=1, maximum=65535),
    ]),
]

SIGNAL_SECTIONS = [
    ("Heartbeat", [
        FieldSpec("heartbeat_address", "PC → PLC heartbeat", "address", tooltip=ADDRESS_HINT),
        FieldSpec("heartbeat_interval_ms", "Heartbeat interval", "int", minimum=100, maximum=60000, suffix="ms"),
        FieldSpec("heartbeat_mode", "Heartbeat mode", "choice", [("TOGGLE", "Toggle 0 / 1"),
                                                                  ("COUNTER", "Counter 0…32767")]),
        FieldSpec("plc_watchdog_address", "PLC → PC watchdog (optional)", "address", tooltip=ADDRESS_HINT,
                  hint="A counter the PLC program increments; stale value = PLC program stopped"),
        FieldSpec("plc_watchdog_timeout_s", "Watchdog timeout", "float", minimum=1, maximum=600, suffix="s"),
    ]),
    ("Alarm", [
        FieldSpec("alarm_address", "Alarm register", "address", tooltip=ADDRESS_HINT),
        FieldSpec("alarm_on_value", "Alarm ON value", "int", minimum=0, maximum=65535),
        FieldSpec("alarm_off_value", "Alarm OFF value", "int", minimum=0, maximum=65535),
        FieldSpec("alarm_code_address", "NG reason code register (optional)", "address", tooltip=ADDRESS_HINT,
                  hint="1 bag, 2 colour, 3 hopper, 4 silo, 5 dist. tank, 6 mold, 7 duplicate, 8 unknown, 10 format"),
        FieldSpec("alarm_reset_mode", "Alarm reset", "choice", [
            ("OPERATOR", "Operator button in this application"),
            ("PLC_ACK", "PLC acknowledgement only"),
            ("OPERATOR_OR_PLC", "Operator button or PLC acknowledgement")]),
        FieldSpec("alarm_ack_address", "PLC acknowledgement signal", "address", tooltip=ADDRESS_HINT),
        FieldSpec("alarm_ack_value", "Acknowledged when value =", "int", minimum=0, maximum=65535),
        FieldSpec("alarm_reset_command_address", "Reset command register (optional)", "address",
                  tooltip=ADDRESS_HINT, hint="Pulsed by the PC when the operator resets the alarm"),
        FieldSpec("alarm_reset_command_value", "Reset command value", "int", minimum=0, maximum=65535),
        FieldSpec("alarm_reset_pulse_ms", "Reset pulse length", "int", minimum=50, maximum=10000, suffix="ms"),
    ]),
    ("Validation Result / Loading Authorisation", [
        FieldSpec("validation_result_enabled", "Write validation result", "bool"),
        FieldSpec("validation_result_address", "Validation result register", "address", tooltip=ADDRESS_HINT),
        FieldSpec("validation_ok_value", "OK value (loading authorised)", "int", minimum=0, maximum=65535),
        FieldSpec("validation_ng_value", "NG value", "int", minimum=0, maximum=65535),
        FieldSpec("validation_idle_value", "Idle value", "int", minimum=0, maximum=65535),
        FieldSpec("hopper_code_address", "Authorised hopper code register", "address", tooltip=ADDRESS_HINT,
                  hint="Receives the PLC code of the hopper whose shutter may open (0 = none)"),
        FieldSpec("destination_code_address", "Destination code register", "address", tooltip=ADDRESS_HINT),
    ]),
    ("Loading Confirmation (Light Curtain / Shutter)", [
        FieldSpec("loading_confirmation_mode", "Completion confirmed by", "choice", [
            ("OPERATOR", "Operator button (no PLC signal)"),
            ("PLC_SIGNAL", "PLC loading-complete signal"),
            ("SHUTTER_CYCLE", "Shutter opened and closed")]),
        FieldSpec("loading_complete_address", "Loading complete signal", "address", tooltip=ADDRESS_HINT),
        FieldSpec("loading_complete_value", "Complete when value =", "int", minimum=0, maximum=65535),
        FieldSpec("shutter_status_address", "Shutter status signal", "address", tooltip=ADDRESS_HINT),
        FieldSpec("shutter_open_value", "Shutter open when value =", "int", minimum=0, maximum=65535),
        FieldSpec("light_curtain_address", "Light curtain signal", "address", tooltip=ADDRESS_HINT),
        FieldSpec("light_curtain_active_value", "Material detected when value =", "int", minimum=0, maximum=65535),
        FieldSpec("loading_timeout_min", "Warn if not confirmed after", "int", minimum=0, maximum=1440, suffix="min"),
    ]),
]

SIM_SECTIONS = [
    ("Simulator Behaviour", [
        FieldSpec("sim_auto_loading", "Simulate PLC loading sequence", "bool",
                  hint="After authorisation: shutter open → light curtain → shutter close → loading complete"),
        FieldSpec("sim_step_delay_s", "Step delay", "float", minimum=0.2, maximum=60, suffix="s"),
    ]),
]

SIGNAL_TABLE = [
    Column("address", "Address", 110),
    Column("value", "Value", 90),
    Column("age", "Age (s)", 80),
    Column("role", "Configured as"),
]


class PlcPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.plc = ctx.plc
        self.root.addWidget(PageHeader(
            "PLC Configuration (Mitsubishi SLMP)",
            "Ethernet connection, heartbeat, alarm, loading authorisation handshake and manual testing."))
        self.root.addWidget(MessageBox(
            "<b>Safety:</b> the PLC controls the hopper shutters and all interlocks. This application only sends "
            "validation results and alarm requests. PLC model, SLMP port and device addresses are not yet "
            "specified - all values below must be agreed with the PLC programmer.", "warn"))

        validator = lambda a: address_problem(a, self.form_connection.values().get("plc_series", "Q_L"))  # noqa: E731
        self.form_connection = ConfigForm(CONNECTION_SECTIONS, columns=2)
        self.form_signals = ConfigForm(SIGNAL_SECTIONS, address_validator=validator, columns=2)
        self.form_sim = ConfigForm(SIM_SECTIONS)
        self.forms = (self.form_connection, self.form_signals, self.form_sim)

        self.tabs = QTabWidget()
        self.tabs.addTab(scroll_wrap(self.form_connection), "Connection")
        self.tabs.addTab(scroll_wrap(self.form_signals), "Signals && Handshake")
        self.tabs.addTab(self._build_status_tab(), "Status && Manual Test")
        self.tabs.addTab(self._build_sim_tab(), "Simulator")
        self.root.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        self.validation = label("", wrap=True)
        bottom.addWidget(self.validation, 1)
        self.btn_revert = button("Revert")
        self.btn_revert.clicked.connect(self._load)
        self.btn_save = button("Save Configuration", "primary")
        self.btn_save.clicked.connect(self._save)
        bottom.addWidget(self.btn_revert)
        bottom.addWidget(self.btn_save)
        self.root.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_status)
        self.plc.status_changed.connect(self._refresh_status)

    # ================================================================== tabs
    def _build_status_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        left = QVBoxLayout()
        status = Card("Communication Status")
        self.status = KeyValueGrid()
        for key, caption in (
            ("connection", "Connection"), ("mode", "Mode"), ("cpu", "CPU type"), ("since", "Connected since"),
            ("heartbeat", "Heartbeat"), ("hb_detail", "Heartbeat detail"), ("errors", "Comm. errors"),
            ("last_error", "Last error"), ("poll", "Last poll"), ("alarm", "Alarm output"),
            ("authorization", "Authorisation output"),
        ):
            self.status.add(key, caption)
        status.body.addWidget(self.status)
        test_btn = button("Test Connection", "primary")
        test_btn.clicked.connect(self._test_connection)
        status.body.addWidget(test_btn, 0, Qt.AlignmentFlag.AlignLeft)
        left.addWidget(status)
        signals = Card("PLC Input Signals (live)")
        self.signal_model = RecordTableModel(SIGNAL_TABLE)
        self.signal_table, _p = make_table(self.signal_model, sortable=False)
        signals.body.addWidget(self.signal_table)
        left.addWidget(signals, 1)
        layout.addLayout(left, 1)

        manual = Card("Manual Read / Write Test")
        manual.body.addWidget(MessageBox(
            "<b>Warning:</b> manual writes go directly to the PLC device memory. Only use during commissioning "
            "with the PLC programmer present and the machine in a safe state.", "warn"))
        row = QHBoxLayout()
        row.addWidget(label("Device", "FieldLabel"))
        self.manual_address = QLineEdit()
        self.manual_address.setPlaceholderText("e.g. D100")
        self.manual_address.setMaximumWidth(160)
        row.addWidget(self.manual_address)
        row.addWidget(label("Value", "FieldLabel"))
        self.manual_value = QSpinBox()
        self.manual_value.setRange(-32768, 65535)
        row.addWidget(self.manual_value)
        self.btn_read = button("Read")
        self.btn_read.clicked.connect(self._manual_read)
        self.btn_write = button("Write", "danger")
        self.btn_write.clicked.connect(self._manual_write)
        row.addWidget(self.btn_read)
        row.addWidget(self.btn_write)
        row.addStretch(1)
        manual.body.addLayout(row)
        self.manual_log = QPlainTextEdit()
        self.manual_log.setReadOnly(True)
        manual.body.addWidget(self.manual_log, 1)
        layout.addWidget(manual, 1)
        return tab

    def _build_sim_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        left = QVBoxLayout()
        self.sim_notice = MessageBox("")
        left.addWidget(self.sim_notice)
        left.addWidget(self.form_sim)
        quick = Card("Simulate PLC Signals")
        quick.body.addWidget(label("Writes into the simulator memory (only available in simulation mode).",
                                   "Muted", wrap=True))
        for text, handler in (
            ("Pulse alarm acknowledgement", self._sim_ack),
            ("Set loading complete", self._sim_complete),
            ("Toggle shutter open / closed", lambda: self._sim_toggle("shutter_status_address", "shutter_open_value")),
            ("Toggle light curtain", lambda: self._sim_toggle("light_curtain_address", "light_curtain_active_value")),
        ):
            btn = button(text)
            btn.clicked.connect(handler)
            quick.body.addWidget(btn)
        left.addWidget(quick)
        left.addStretch(1)
        layout.addLayout(left, 1)

        memory = Card("Simulator Device Memory")
        row = QHBoxLayout()
        self.sim_address = QLineEdit()
        self.sim_address.setPlaceholderText("Device, e.g. X20")
        self.sim_address.setMaximumWidth(150)
        self.sim_value = QSpinBox()
        self.sim_value.setRange(0, 65535)
        set_btn = button("Set Value")
        set_btn.clicked.connect(self._sim_set)
        row.addWidget(self.sim_address)
        row.addWidget(self.sim_value)
        row.addWidget(set_btn)
        row.addStretch(1)
        memory.body.addLayout(row)
        self.sim_model = RecordTableModel([Column("address", "Device", 120), Column("value", "Value")])
        self.sim_table, _p = make_table(self.sim_model)
        memory.body.addWidget(self.sim_table, 1)
        layout.addWidget(memory, 1)
        return tab

    # ================================================================== page
    def on_show(self) -> None:
        self._load()
        self._refresh_status()
        self._timer.start()
        self.on_user_changed()

    def on_hide(self) -> None:
        self._timer.stop()

    def on_user_changed(self) -> None:
        can_test = self.ctx.auth.has_permission(Permission.PLC_MANUAL_TEST)
        self.btn_write.setEnabled(can_test)

    def can_leave(self) -> bool:
        if any(form.is_dirty() for form in self.forms):
            return confirm(self, "Unsaved Changes", "Discard the unsaved PLC configuration changes?",
                           yes_text="Discard")
        return True

    def _load(self) -> None:
        cfg = self.ctx.config.plc()
        for form in self.forms:
            form.load_dataclass(cfg)
        self._show_validation(*validate_plc_config(cfg))

    def _collect(self) -> PlcConfig:
        cfg = self.ctx.config.plc()
        for form in self.forms:
            cfg = form.apply_to(cfg)
        for field_name in ("heartbeat_address", "plc_watchdog_address", "alarm_address", "alarm_code_address",
                           "alarm_ack_address", "alarm_reset_command_address", "validation_result_address",
                           "hopper_code_address", "destination_code_address", "loading_complete_address",
                           "shutter_status_address", "light_curtain_address"):
            setattr(cfg, field_name, getattr(cfg, field_name).strip().upper())
        return cfg

    def _show_validation(self, errors: list[str], warnings: list[str]) -> None:
        parts = [f"<span style='color:#B91C1C'>✖ {e}</span>" for e in errors]
        parts += [f"<span style='color:#C2410C'>⚠ {w}</span>" for w in warnings]
        self.validation.setText("<br>".join(parts))

    def _save(self) -> None:
        cfg = self._collect()
        errors, warnings = validate_plc_config(cfg)
        self._show_validation(errors, warnings)
        if errors:
            show_error(self, "PLC Configuration", "Please correct the errors:\n\n" + "\n".join(errors))
            return
        if warnings and not confirm(self, "PLC Configuration",
                                    "Save with the following warnings?\n\n" + "\n".join(warnings)):
            return
        changed = self.ctx.config.save("plc", cfg, self.username)
        for form in self.forms:
            form.mark_clean()
        show_info(self, "PLC Configuration",
                  "Configuration saved and applied." if changed else "No changes.")

    # ================================================================== status
    def _refresh_status(self) -> None:
        if not self.isVisible():
            return
        plc = self.plc
        cfg = plc.config
        stats = plc.stats or {}
        s = self.status
        if not cfg.enabled:
            s.set("connection", "Disabled", "neutral")
        elif plc.connected:
            s.set("connection", plc.status_text, "ok")
        else:
            s.set("connection", f"Disconnected – {plc.status_text}", "ng")
        mode = "Simulator" if plc.simulated else f"{cfg.transport} {cfg.host}:{cfg.port} ({cfg.frame_type} " \
                                                  f"{cfg.data_code}, {cfg.plc_series})"
        if plc.forced_simulation:
            mode += "  [forced by --simulate]"
        s.set("mode", mode)
        s.set("cpu", stats.get("cpu_model", ""))
        since = stats.get("connected_since")
        s.set("since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)) if since and plc.connected else "—")
        s.set("heartbeat", *heartbeat_label(plc.heartbeat_state))
        s.set("hb_detail", plc.heartbeat_detail)
        s.set("errors", f"{stats.get('comm_errors', 0)} errors, {stats.get('connect_failures', 0)} failed connects")
        s.set("last_error", stats.get("last_error", ""))
        s.set("poll", f"{stats.get('last_poll_ms', 0)} ms")
        s.set("alarm", f"ACTIVE (code {plc.alarm_code})" if plc.alarm_active else "Off",
              "ng" if plc.alarm_active else "ok")
        s.set("authorization", f"Hopper code {plc.authorized[0]}, destination {plc.authorized[1]}"
              if plc.authorized else "None", "ok" if plc.authorized else None)
        roles = {}
        for field_name, role in (("plc_watchdog_address", "PLC watchdog"), ("alarm_ack_address", "Alarm ack"),
                                 ("loading_complete_address", "Loading complete"),
                                 ("shutter_status_address", "Shutter status"),
                                 ("light_curtain_address", "Light curtain")):
            address = getattr(cfg, field_name).strip()
            if address:
                roles[plc.normalize_address(address)] = role
        rows = []
        for address, (value, age) in sorted(plc.cache_snapshot().items()):
            rows.append({"address": address, "value": "error" if value is None else value,
                         "age": f"{age:.1f}", "role": roles.get(address, "Equipment limit switch")})
        self.signal_model.set_rows(rows)
        self.sim_notice.set_text(
            "The built-in simulator is <b>active</b>." if plc.simulated else
            "The simulator is <b>not active</b> (real PLC mode). These controls have no effect.")
        self.sim_model.set_rows([{"address": k, "value": v} for k, v in sorted(plc.sim_memory.snapshot().items())])

    def _log(self, text: str) -> None:
        self.manual_log.appendPlainText(f"{fmt_time(None) or time.strftime('%H:%M:%S')}  {text}")

    def _test_connection(self) -> None:
        self._log("Testing connection…")

        def done(ok, value, error):
            self._log(f"OK – {value}" if ok else f"FAILED – {error}")
            if not ok:
                show_warning(self, "PLC Connection Test", error)

        self.plc.test_connection(done)

    def _manual_address(self) -> str:
        address = self.manual_address.text().strip().upper()
        problem = address_problem(address, self.plc.series) if address else "Enter a device address."
        if problem:
            show_error(self, "Manual Test", problem)
            return ""
        return address

    def _manual_read(self) -> None:
        address = self._manual_address()
        if not address:
            return

        def done(ok, value, error):
            if ok:
                self._log(f"READ  {address} = {value} (signed {to_signed16(value)}, hex 0x{value:04X})")
            else:
                self._log(f"READ  {address} failed: {error}")

        self.plc.read_device(address, done)

    def _manual_write(self) -> None:
        if not self.ctx.auth.has_permission(Permission.PLC_MANUAL_TEST):
            return
        address = self._manual_address()
        if not address:
            return
        value = self.manual_value.value()
        if not confirm(self, "Manual PLC Write",
                       f"Write {value} to {address} on the {'simulator' if self.plc.simulated else 'PLC'}?\n\n"
                       "Make sure the machine is in a safe state.", danger=not self.plc.simulated, yes_text="Write"):
            return
        self.ctx.audit.log("PLC", "MANUAL_WRITE", f"Manual write {address} = {value}", entity="plc",
                           entity_id=address)

        def done(ok, _value, error):
            self._log(f"WRITE {address} = {value}: " + ("OK" if ok else f"failed: {error}"))

        self.plc.write_device(address, value & 0xFFFF, done)

    # ================================================================== simulator
    def _sim_key(self, address: str) -> str:
        return self.plc.normalize_address(address) if address.strip() else ""

    def _sim_ack(self) -> None:
        cfg = self.plc.config
        key = self._sim_key(cfg.alarm_ack_address)
        if not (self.plc.simulated and key):
            show_info(self, "Simulator", "Configure the alarm acknowledgement address and enable simulation.")
            return
        self.plc.sim_memory.write(key, cfg.alarm_ack_value)
        QTimer.singleShot(1500, lambda: self.plc.sim_memory.write(key, 0 if cfg.alarm_ack_value else 1))

    def _sim_complete(self) -> None:
        cfg = self.plc.config
        key = self._sim_key(cfg.loading_complete_address)
        if not (self.plc.simulated and key):
            show_info(self, "Simulator", "Configure the loading-complete address and enable simulation.")
            return
        self.plc.sim_memory.write(key, cfg.loading_complete_value)

    def _sim_toggle(self, address_field: str, value_field: str) -> None:
        cfg = self.plc.config
        key = self._sim_key(getattr(cfg, address_field))
        if not (self.plc.simulated and key):
            show_info(self, "Simulator", "Configure the signal address and enable simulation.")
            return
        active = getattr(cfg, value_field)
        current = self.plc.sim_memory.read(key)
        self.plc.sim_memory.write(key, (0 if active else 1) if current == active else active)

    def _sim_set(self) -> None:
        address = self.sim_address.text().strip()
        problem = address_problem(address, self.plc.series) if address else "Enter a device address."
        if problem:
            show_error(self, "Simulator", problem)
            return
        self.plc.sim_memory.write(self.plc.normalize_address(address), self.sim_value.value())
        self._refresh_status()
