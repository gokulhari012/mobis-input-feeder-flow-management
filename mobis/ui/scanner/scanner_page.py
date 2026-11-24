"""Barcode scanner configuration and test page."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QListWidget, QVBoxLayout, QWidget

from mobis.core.config_models import ScannerConfig
from mobis.hardware.barcode_scanner import list_serial_ports
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
)
from mobis.ui.widgets.config_form import ConfigForm, FieldSpec
from mobis.ui.widgets.scan_input import ScanInputField
from mobis.utils.timeutil import fmt_datetime, fmt_time

BAUD_RATES = [(b, str(b)) for b in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)]

SECTIONS = [
    ("Connection", [
        FieldSpec("mode", "Scanner connection type", "choice", [
            ("HID_KEYBOARD", "USB HID keyboard wedge (default)"),
            ("SERIAL", "Serial / virtual COM port"),
            ("SIMULATION", "Simulation (no hardware)"),
        ], tooltip="Depends on how the wireless scanner base station is configured."),
    ]),
    ("Keyboard Wedge (HID)", [
        FieldSpec("hid_terminator", "Terminator", "choice", [("ENTER", "Enter (CR)"), ("TAB", "Tab"),
                                                            ("NONE", "None (idle timeout)")]),
        FieldSpec("hid_max_interkey_ms", "Scanner detection", "int", minimum=5, maximum=1000, suffix="ms",
                  hint="Keystrokes faster than this are treated as scanner input; slower as manual typing"),
        FieldSpec("hid_idle_submit_ms", "Idle timeout", "int", minimum=30, maximum=5000, suffix="ms"),
        FieldSpec("allow_manual_entry", "Allow manual entry", "bool",
                  tooltip="When disabled, slowly typed barcodes are rejected on the operation screen"),
    ]),
    ("Serial / COM Port", [
        FieldSpec("serial_port", "COM port", "combo_edit"),
        FieldSpec("baud_rate", "Baud rate", "choice", BAUD_RATES),
        FieldSpec("data_bits", "Data bits", "choice", [(7, "7"), (8, "8")]),
        FieldSpec("parity", "Parity", "choice", [("N", "None"), ("E", "Even"), ("O", "Odd"), ("M", "Mark"),
                                                 ("S", "Space")]),
        FieldSpec("stop_bits", "Stop bits", "choice", [("1", "1"), ("1.5", "1.5"), ("2", "2")]),
        FieldSpec("serial_terminator", "Terminator", "choice", [
            ("CR_OR_LF", "CR or LF"), ("CR", "CR"), ("LF", "LF"), ("CRLF", "CR+LF"),
            ("CUSTOM", "Custom (hex)"), ("NONE", "None (idle timeout)")]),
        FieldSpec("custom_terminator_hex", "Custom terminator (hex)", placeholder="e.g. 03"),
        FieldSpec("encoding", "Encoding", "choice", [("utf-8", "UTF-8"), ("ascii", "ASCII"), ("latin-1", "Latin-1")]),
        FieldSpec("serial_idle_submit_ms", "Idle timeout", "int", minimum=20, maximum=5000, suffix="ms"),
        FieldSpec("serial_reconnect_s", "Reconnect interval", "float", minimum=1, maximum=600, suffix="s"),
    ]),
    ("Data Format", [
        FieldSpec("prefix", "Prefix to strip", placeholder="e.g. <STX> or ]C1",
                  hint="Supports <STX>, <ETX>, \\x02, \\r, \\n"),
        FieldSpec("suffix", "Suffix to strip", placeholder="e.g. <ETX>"),
        FieldSpec("strip_whitespace", "Trim spaces", "bool"),
        FieldSpec("remove_control_chars", "Remove control characters", "bool"),
        FieldSpec("gs_replacement", "Replace GS (0x1D) with", placeholder="e.g. |"),
        FieldSpec("uppercase", "Convert to upper case", "bool"),
        FieldSpec("min_length", "Minimum length", "int", minimum=1, maximum=500),
        FieldSpec("max_length", "Maximum length", "int", minimum=1, maximum=2000),
        FieldSpec("duplicate_window_ms", "Duplicate scan window", "int", minimum=0, maximum=60000, suffix="ms",
                  hint="Identical scans within this time are ignored (scanner double reads)"),
    ]),
]


class ScannerPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        header = PageHeader("Barcode Scanner", "Wireless scanner connection, data format and live test.")
        self.root.addWidget(header)
        self.root.addWidget(MessageBox(
            "Scanner model and output mode are not yet specified. Most wireless scanners work as <b>USB HID "
            "keyboard wedge</b> (default). Select <b>Serial / COM port</b> if the base station is configured as a "
            "virtual COM port (CDC / VCP)."))

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.form = ConfigForm(SECTIONS, columns=2)
        left_layout.addWidget(self.form)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.btn_revert = button("Revert")
        self.btn_revert.clicked.connect(self._load)
        self.btn_save = button("Save Configuration", "primary")
        self.btn_save.clicked.connect(self._save)
        actions.addWidget(self.btn_revert)
        actions.addWidget(self.btn_save)
        left_layout.addLayout(actions)
        body.addWidget(scroll_wrap(left), 3)

        right = QVBoxLayout()
        status = Card("Scanner Status")
        self.status = KeyValueGrid()
        for key, caption in (("mode", "Mode"), ("state", "State"), ("detail", "Detail"),
                             ("last", "Last barcode"), ("source", "Source"), ("time", "Received at")):
            self.status.add(key, caption)
        status.body.addWidget(self.status)
        row = QHBoxLayout()
        refresh_ports = button("Refresh COM Ports")
        refresh_ports.clicked.connect(self._refresh_ports)
        test_connection = button("Test Connection")
        test_connection.clicked.connect(self._test_connection)
        row.addWidget(refresh_ports)
        row.addWidget(test_connection)
        row.addStretch(1)
        status.body.addLayout(row)
        right.addWidget(status)

        test = Card("Test Scanner Input")
        test.body.addWidget(label("Scan a barcode now. Test scans are NOT processed as material transactions.",
                                  "Muted", wrap=True))
        self.test_field = ScanInputField()
        self.test_field.setPlaceholderText("Scan here to test…")
        self.test_field.submitted.connect(
            lambda text, avg, n: self.ctx.scanner.submit_keyboard(text, avg, n))
        test.body.addWidget(self.test_field)
        self.received = QListWidget()
        test.body.addWidget(self.received, 1)
        clear = button("Clear")
        clear.clicked.connect(self.received.clear)
        test.body.addWidget(clear)
        right.addWidget(test, 1)
        body.addLayout(right, 2)
        self.root.addLayout(body, 1)

        ctx.scanner.test_scan.connect(self._on_test_scan)
        ctx.scanner.rejected.connect(self._on_rejected)
        ctx.scanner.status_changed.connect(self._render_status)

    # ------------------------------------------------------------------ page
    def on_show(self) -> None:
        self.ctx.scanner.test_mode = True
        self._refresh_ports()
        self._load()
        self._render_status()
        self.test_field.setFocus()

    def on_hide(self) -> None:
        self.ctx.scanner.test_mode = False

    def can_leave(self) -> bool:
        if self.form.is_dirty():
            return confirm(self, "Unsaved Changes", "Discard the unsaved scanner configuration changes?",
                           yes_text="Discard")
        return True

    def _load(self) -> None:
        cfg = self.ctx.config.scanner()
        self.form.load_dataclass(cfg)
        self.test_field.configure(cfg.hid_terminator, cfg.hid_idle_submit_ms)

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.form.set_choices("serial_port", [(device, f"{device} – {desc}") for device, desc in ports])

    def _save(self) -> None:
        cfg: ScannerConfig = self.form.apply_to(self.ctx.config.scanner())
        if cfg.mode == "SERIAL" and not cfg.serial_port:
            show_error(self, "Scanner Configuration", "Select the COM port for serial mode.")
            return
        if cfg.min_length > cfg.max_length:
            show_error(self, "Scanner Configuration", "Minimum length cannot exceed maximum length.")
            return
        if cfg.serial_terminator == "CUSTOM":
            try:
                if not bytes.fromhex(cfg.custom_terminator_hex.replace(" ", "")):
                    raise ValueError
            except ValueError:
                show_error(self, "Scanner Configuration", "Enter the custom terminator as hex bytes, e.g. 03.")
                return
        changed = self.ctx.config.save("scanner", cfg, self.username)
        self.form.mark_clean()
        self.test_field.configure(cfg.hid_terminator, cfg.hid_idle_submit_ms)
        show_info(self, "Scanner Configuration", "Configuration saved." if changed else "No changes.")

    def _test_connection(self) -> None:
        scanner = self.ctx.scanner
        if scanner.mode == "SERIAL":
            scanner.stop()
            scanner.start()
            show_info(self, "Test Connection", "The COM port is being re-opened; see the scanner status.")
        else:
            self.test_field.setFocus()
            show_info(self, "Test Connection",
                      "HID keyboard scanners do not report a connection state. Scan a barcode into the test field "
                      "to verify that the scanner and its data format work.")

    # ------------------------------------------------------------------ live
    def _on_test_scan(self, barcode: str, source: str, raw: str) -> None:
        raw_hex = raw.encode("utf-8", errors="replace").hex(" ").upper()
        self.received.insertItem(0, f"{fmt_time(self.ctx.scanner.last_time)}  [{source}]  {barcode}    (raw: {raw_hex})")
        self._render_status()

    def _on_rejected(self, raw: str, reason: str) -> None:
        if self.isVisible():
            self.received.insertItem(0, f"REJECTED: {reason}  (raw: {raw!r})")

    def _render_status(self) -> None:
        s = self.ctx.scanner
        level, text = s.status()
        self.status.set("mode", {"HID_KEYBOARD": "USB HID keyboard wedge", "SERIAL": "Serial / COM port",
                                 "SIMULATION": "Simulation"}.get(s.mode, s.mode))
        self.status.set("state", text, level)
        self.status.set("detail", s.detail)
        self.status.set("last", s.last_barcode)
        self.status.set("source", s.last_source)
        self.status.set("time", fmt_datetime(s.last_time))
