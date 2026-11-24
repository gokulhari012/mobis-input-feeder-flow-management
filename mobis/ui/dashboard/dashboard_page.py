"""Operation screen used by factory operators.

Priorities: bag type selection, scanning without clicking, unmistakable OK / NG
feedback, expected vs. scanned equipment, and the current process stage.
Configuration details are deliberately kept off this screen.
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from mobis.app.styles import LEVELS, SOFT
from mobis.core.models import BarcodeMatchType, CheckItem, CheckResult, Stage
from mobis.core.permissions import Permission
from mobis.core.process_state_machine import LOADING_STATES, SCANNING_STATES, ProcessState as S
from mobis.core.transaction_manager import ProcessContext
from mobis.hardware.plc_manager import heartbeat_label
from mobis.ui.base_page import BasePage
from mobis.ui.dialogs import AuthorizeDialog
from mobis.ui.widgets.common import (
    Card,
    KeyValueGrid,
    LevelBar,
    ResultBanner,
    ResultChip,
    button,
    confirm,
    fmt_kg,
    font,
    label,
    scroll_wrap,
)
from mobis.ui.widgets.scan_input import ScanInputField
from mobis.utils.timeutil import fmt_time

STEP_COLOURS = {
    "done": ("#14532D", "#DCFCE7", "#86EFAC"),
    "current": ("#FFFFFF", "#1D4ED8", "#1D4ED8"),
    "failed": ("#FFFFFF", "#B91C1C", "#B91C1C"),
    "waiting": ("#7C2D12", "#FFEDD5", "#FDBA74"),
    "pending": ("#64748B", "#F8FAFC", "#E2E8F0"),
}
CONFIRMATION_TEXT = {
    "OPERATOR": "Operator confirms with LOADING COMPLETE",
    "PLC_SIGNAL": "PLC loading-complete signal",
    "SHUTTER_CYCLE": "PLC shutter open / close cycle",
}


class StepIndicator(QWidget):
    """Horizontal process timeline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._titles: list[str] = []
        self._labels = []

    def set_steps(self, steps: list[tuple[str, str]]) -> None:
        titles = [t for t, _s in steps]
        if titles != self._titles:
            while self._layout.count():
                item = self._layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)  # detach now; deleteLater alone keeps it painted until deletion
                    widget.deleteLater()
            self._labels = []
            for i, title in enumerate(titles):
                if i:
                    arrow = label("›")
                    arrow.setFont(font(14, True))
                    arrow.setStyleSheet("color: #94A3B8;")
                    self._layout.addWidget(arrow)
                lbl = label(title)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setFont(font(9.5, True))
                lbl.setMinimumHeight(30)
                self._layout.addWidget(lbl, 1)
                self._labels.append(lbl)
            self._titles = titles
        for lbl, (_title, status) in zip(self._labels, steps):
            fg, bg, border = STEP_COLOURS.get(status, STEP_COLOURS["pending"])
            lbl.setStyleSheet(
                f"color: {fg}; background: {bg}; border: 1px solid {border}; border-radius: 15px; padding: 2px 8px;"
            )


class DashboardPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.tm = ctx.transactions
        self._sim_counter = 0
        self._sim_bag_type_id: Optional[int] = None

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.root.addWidget(scroll_wrap(content))

        # ------------------------------------------------------------ control bar
        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(label("BAG TYPE", "SectionLabel"))
        self.bag_combo = QComboBox()
        self.bag_combo.setObjectName("BagTypeCombo")
        self.bag_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.bag_combo.setMinimumContentsLength(22)  # may shrink on small screens instead of overflowing
        self.bag_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.bag_combo.activated.connect(self._on_bag_selected)
        bar.addWidget(self.bag_combo, 2)
        bar.addStretch(1)
        self.btn_retry = button("Retry Authorisation", large=True, focusable=False)
        self.btn_retry.clicked.connect(self.tm.retry_authorization)
        self.btn_force = button("Force Complete…", "warning", large=True, focusable=False,
                                tooltip="Supervisor override when the PLC confirmation signal is not available")
        self.btn_force.clicked.connect(self._on_force_complete)
        self.btn_complete = button("LOADING COMPLETE", "success", large=True, focusable=False)
        self.btn_complete.clicked.connect(self._on_complete)
        for btn in (self.btn_retry, self.btn_force, self.btn_complete):
            bar.addWidget(btn)
        self.btn_new = button("New Transaction", "primary", large=True, focusable=False)
        self.btn_new.clicked.connect(self._on_new)
        self.btn_cancel = button("Cancel Transaction", large=True, focusable=False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_reset = button("Reset Alarm", "danger", large=True, focusable=False)
        self.btn_reset.clicked.connect(self._on_reset_alarm)
        for btn in (self.btn_new, self.btn_cancel, self.btn_reset):
            bar.addWidget(btn)
        layout.addLayout(bar)

        # ------------------------------------------------------------ banner + scan field
        self.banner = ResultBanner()
        layout.addWidget(self.banner)

        scan_card = Card()
        scan_row = QHBoxLayout()
        scan_row.setSpacing(12)
        scan_caption = label("SCAN")
        scan_caption.setFont(font(16, True))
        scan_caption.setStyleSheet("color: #1D4ED8;")
        scan_row.addWidget(scan_caption)
        self.scan_field = ScanInputField()
        self.scan_field.submitted.connect(self._on_scan_field)
        scan_row.addWidget(self.scan_field, 1)
        self.next_chip = ResultChip()
        self.next_chip.setMinimumWidth(200)
        self.next_chip.setFont(font(12, True))
        scan_row.addWidget(self.next_chip)
        scan_card.body.addLayout(scan_row)
        self.steps = StepIndicator()
        scan_card.body.addWidget(self.steps)
        self.notice = label("", wrap=True)
        self.notice.setFont(font(10.5))
        scan_card.body.addWidget(self.notice)
        layout.addWidget(scan_card)

        # ------------------------------------------------------------ verification + side panels
        middle = QHBoxLayout()
        middle.setSpacing(12)

        verify = Card("Verification")
        self.tx_label = label("", "Muted")
        verify.add_header_widget(self.tx_label)
        self.stage_table = QTableWidget(0, 5)
        self.stage_table.setObjectName("StageTable")
        self.stage_table.setHorizontalHeaderLabels(["Check", "Expected", "Scanned", "Result", "Remarks"])
        vertical = self.stage_table.verticalHeader()
        vertical.setVisible(False)
        vertical.setMinimumSectionSize(44)
        vertical.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)  # rows grow with the card
        self.stage_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stage_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.stage_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.stage_table.setShowGrid(False)
        self.stage_table.setWordWrap(True)
        header = self.stage_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.stage_table.setColumnWidth(3, 110)
        self.stage_table.setMinimumHeight(6 * 44 + 36)
        verify.body.addWidget(self.stage_table)

        # Logic matrix of the specification: overall OK only when every item is OK (or N/A).
        matrix = QGridLayout()
        matrix.setHorizontalSpacing(8)
        matrix.setVerticalSpacing(2)
        matrix.addWidget(label("LOGIC\nMATRIX", "SectionLabel"), 0, 0, 2, 1)
        self.matrix_chips: dict[CheckItem, ResultChip] = {}
        self.matrix_captions: dict[CheckItem, QWidget] = {}
        captions = {CheckItem.COLOUR: "Colour", CheckItem.HOPPER: "Hopper", CheckItem.SILO: "Silo",
                    CheckItem.DISTRIBUTION_TANK: "Dist. Tank", CheckItem.MOLD: "Mold"}
        for column, (item, caption) in enumerate(captions.items(), start=1):
            cap = label(caption, "Muted")
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip = ResultChip()
            chip.setMinimumWidth(56)
            matrix.addWidget(cap, 0, column)
            matrix.addWidget(chip, 1, column)
            self.matrix_chips[item] = chip
            self.matrix_captions[item] = cap
        matrix.setColumnStretch(len(captions) + 1, 1)
        overall_caption = label("OVERALL", "SectionLabel")
        overall_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        matrix.addWidget(overall_caption, 0, len(captions) + 2)
        self.overall_chip = ResultChip()
        self.overall_chip.setMinimumWidth(110)
        self.overall_chip.setFont(font(12, True))
        matrix.addWidget(self.overall_chip, 1, len(captions) + 2)
        verify.body.addLayout(matrix)
        middle.addWidget(verify, 3)

        side = QVBoxLayout()
        side.setSpacing(12)
        material = Card("Material & Quantity")
        self.material_grid = KeyValueGrid(columns=2)
        for key, caption in (
            ("bag_type", "Bag type"), ("colour", "Colour"),
            ("material", "Material"), ("weight", "Bag weight"),
            ("hopper", "Hopper"), ("silo", "Silo"),
            ("distribution", "Dist. tank"), ("destination", "Destination"),
            ("existing", "Existing qty"), ("total", "Total (est.)"),
        ):
            self.material_grid.add(key, caption)
        material.body.addWidget(self.material_grid)
        self.level_bar = LevelBar()
        material.body.addWidget(self.level_bar)
        side.addWidget(material)

        status = Card("System Status & Loading Authorisation")
        self.status_grid = KeyValueGrid(columns=2)
        for key, caption in (
            ("system", "System"), ("plc", "PLC"),
            ("heartbeat", "Heartbeat"), ("scanner", "Scanner"),
            ("mes", "MES"), ("pending", "Pending MES"),
            ("last", "Last loading"),
        ):
            self.status_grid.add(key, caption)
        status.body.addWidget(self.status_grid)
        self.readiness_label = label("", wrap=True)
        self.readiness_label.setTextFormat(Qt.TextFormat.RichText)
        status.body.addWidget(self.readiness_label)
        self.loading_info = label("", "Muted", wrap=True)
        status.body.addWidget(self.loading_info)
        side.addWidget(status)
        side.addStretch(1)
        middle.addLayout(side, 2)
        layout.addLayout(middle, 1)

        # ------------------------------------------------------------ scanner simulation
        self.sim_card = Card("Scanner Simulation (testing without hardware)")
        sim_row = QHBoxLayout()
        self.sim_combo = QComboBox()
        self.sim_combo.setMinimumWidth(360)
        self.sim_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        sim_row.addWidget(self.sim_combo, 2)
        sim_send = button("Send Scan", "primary", focusable=False)
        sim_send.clicked.connect(self._send_sim_choice)
        sim_row.addWidget(sim_send)
        sim_row.addSpacing(16)
        self.sim_text = QLineEdit()
        self.sim_text.setPlaceholderText("Any barcode text…")
        self.sim_text.returnPressed.connect(self._send_sim_text)
        sim_row.addWidget(self.sim_text, 1)
        sim_text_btn = button("Send", focusable=False)
        sim_text_btn.clicked.connect(self._send_sim_text)
        sim_row.addWidget(sim_text_btn)
        self.sim_card.body.addLayout(sim_row)
        layout.addWidget(self.sim_card)

        # ------------------------------------------------------------ wiring
        self.tm.context_changed.connect(self.render)
        self.tm.notice.connect(self.show_notice)
        ctx.scanner.rejected.connect(self._on_scan_rejected)
        ctx.scanner.status_changed.connect(self._render_status)
        ctx.plc.status_changed.connect(self._render_status)
        ctx.plc.status_changed.connect(self._render_readiness)
        ctx.mes.status_changed.connect(self._render_status)
        ctx.hub.bag_types_changed.connect(self.reload_bag_types)
        ctx.hub.equipment_changed.connect(self._rebuild_sim_choices)
        ctx.config.changed.connect(self._on_config_changed)

        self._focus_timer = QTimer(self)
        self._focus_timer.setInterval(400)
        self._focus_timer.timeout.connect(self._ensure_scan_focus)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(lambda: self._set_notice("", "neutral"))

        self._apply_scanner_config()
        self.reload_bag_types()
        self.render()

    # ================================================================== lifecycle
    def on_show(self) -> None:
        self._focus_timer.start()
        self.sim_card.setVisible(self.ctx.scanner.simulation_enabled)
        self.render()
        self.scan_field.setFocus()

    def on_hide(self) -> None:
        self._focus_timer.stop()

    def _ensure_scan_focus(self) -> None:
        """Keep the scan field focused so operators never need to click it."""
        if not self.isVisible() or QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return
        if not self.window().isActiveWindow():
            return
        focused = QApplication.focusWidget()
        if focused is self.scan_field:
            return
        if isinstance(focused, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit)) and self.isAncestorOf(focused):
            return  # e.g. the simulation text field
        self.scan_field.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_config_changed(self, section: str) -> None:
        if section == "scanner":
            self._apply_scanner_config()
            self.sim_card.setVisible(self.ctx.scanner.simulation_enabled)
        self._render_status()

    def _apply_scanner_config(self) -> None:
        cfg = self.ctx.scanner.config
        self.scan_field.configure(cfg.hid_terminator, cfg.hid_idle_submit_ms)

    # ================================================================== bag types
    def reload_bag_types(self) -> None:
        try:
            options = self.tm.bag_type_options()
        except Exception:  # noqa: BLE001
            options = []
        self.bag_combo.blockSignals(True)
        self.bag_combo.clear()
        self.bag_combo.addItem("— Select bag type —", None)
        for bag_type in options:
            self.bag_combo.addItem(f"{bag_type.code}  –  {bag_type.name}   ({bag_type.weight_kg:g} kg)", bag_type.id)
        self.bag_combo.blockSignals(False)
        self._sync_combo()
        self._rebuild_sim_choices()

    def _sync_combo(self) -> None:
        bag_type = self.tm.ctx.bag_type
        index = self.bag_combo.findData(bag_type.id) if bag_type else 0
        self.bag_combo.blockSignals(True)
        self.bag_combo.setCurrentIndex(max(0, index))
        self.bag_combo.blockSignals(False)

    def _on_bag_selected(self, index: int) -> None:
        bag_type_id = self.bag_combo.itemData(index)
        if bag_type_id is None:
            if not self.tm.is_transaction_open:
                self.tm.cancel()
            self._sync_combo()
            return
        ok, message = self.tm.select_bag_type(bag_type_id)
        if not ok:
            self.show_notice("ng", message)
        self._sync_combo()
        self.scan_field.setFocus()

    # ================================================================== actions
    def _on_scan_field(self, text: str, average_ms: float, key_count: int) -> None:
        self.ctx.scanner.submit_keyboard(text, average_ms, key_count)

    def _on_scan_rejected(self, raw: str, reason: str) -> None:
        if not self.ctx.scanner.test_mode:
            self.show_notice("warn", f"Scan not accepted: {reason}")

    def _on_new(self) -> None:
        ok, message = self.tm.new_transaction()
        if not ok:
            self.show_notice("warn", message)

    def _on_cancel(self) -> None:
        tm = self.tm
        if tm.is_transaction_open and tm.state not in (S.LOADING_COMPLETED, S.CANCELLED):
            text = f"Cancel transaction {tm.ctx.transaction_no}?"
            if tm.state in LOADING_STATES:
                text += ("\n\nLoading has already been authorised. The authorisation will be revoked and the tank "
                         "quantity will NOT be updated. Verify the physical state of the hopper.")
            if self.ctx.config.app().confirm_cancel and not confirm(
                self, "Cancel Transaction", text, danger=True, yes_text="Cancel Transaction"
            ):
                return
        ok, message = tm.cancel("Cancelled by operator")
        if not ok:
            self.show_notice("warn", message)

    def _on_reset_alarm(self) -> None:
        ok, message = self.tm.reset_alarm("OPERATOR")
        if not ok:
            self.show_notice("warn", message)

    def _on_complete(self) -> None:
        hopper = self.tm.ctx.hopper.name if self.tm.ctx.hopper else "the hopper"
        if not confirm(self, "Loading Complete",
                       f"Confirm that the complete bag has been filled into {hopper}?", yes_text="Confirm"):
            return
        ok, message = self.tm.confirm_loading_complete()
        if not ok:
            self.show_notice("warn", message)

    def _on_force_complete(self) -> None:
        auth = self.ctx.auth
        if auth.has_permission(Permission.FORCE_COMPLETE):
            if not confirm(self, "Force Complete",
                           "The PLC has not confirmed loading completion.\n\nForce completion and update the tank "
                           "quantity? This action is recorded in the audit log.", danger=True,
                           yes_text="Force Complete"):
                return
            forced_by = auth.audit_username()
        else:
            dialog = AuthorizeDialog(
                auth, Permission.FORCE_COMPLETE, "Supervisor Authorisation",
                "The PLC has not confirmed loading completion.<br>A supervisor or administrator must authorise "
                "<b>forced completion</b> (recorded in the audit log).", self,
            )
            if dialog.exec() != dialog.DialogCode.Accepted or dialog.user is None:
                return
            forced_by = dialog.user.username
        ok, message = self.tm.confirm_loading_complete(forced_by=forced_by)
        if not ok:
            self.show_notice("warn", message)

    # ================================================================== notices
    def show_notice(self, level: str, message: str) -> None:
        self._set_notice(message, level)
        self._notice_timer.start(7000)

    def _set_notice(self, message: str, level: str) -> None:
        if not message:
            last = self.ctx.scanner.last_barcode
            when = fmt_time(self.ctx.scanner.last_time)
            message = f"Last scan: {last}  ({when})" if last else "Ready - scan the barcode, no need to click the field."
            level = "neutral"
        colour = {"ng": "#B91C1C", "warn": "#C2410C", "ok": "#15803D", "info": "#1D4ED8"}.get(level, "#5B6776")
        self.notice.setStyleSheet(f"color: {colour}; font-weight: {600 if level != 'neutral' else 400};")
        self.notice.setText(message)

    # ================================================================== rendering
    def render(self) -> None:
        ctx = self.tm.ctx
        state = ctx.state
        self.banner.set_state(ctx.banner.level, ctx.banner.title, ctx.banner.message)
        self._sync_combo()
        self.tx_label.setText(f"Transaction {ctx.transaction_no}" if ctx.transaction_no else "")

        # Only the buttons that apply to the current state are shown (keeps the bar compact).
        open_tx = self.tm.is_transaction_open and state not in (S.LOADING_COMPLETED, S.CANCELLED)
        self.bag_combo.setEnabled(not open_tx)
        self.btn_new.setVisible(not open_tx and ctx.bag_type is not None)
        self.btn_cancel.setVisible(open_tx or state == S.BAG_TYPE_SELECTED)
        self.btn_cancel.setText("Cancel Transaction" if open_tx else "Clear Selection")
        reset_mode = self.ctx.config.plc().alarm_reset_mode
        self.btn_reset.setVisible(ctx.alarm_active)
        self.btn_reset.setEnabled(reset_mode != "PLC_ACK")
        self.btn_reset.setText("Reset Alarm" if reset_mode != "PLC_ACK" else "Acknowledge on PLC panel")
        loading = state in LOADING_STATES
        operator_mode = ctx.confirmation_mode == "OPERATOR"
        self.btn_complete.setVisible(loading and operator_mode)
        self.btn_force.setVisible(loading and not operator_mode)
        self.btn_retry.setVisible(state == S.VALIDATION_PASSED)

        if ctx.alarm_active:
            self.next_chip.set_level("ng", "ALARM")
        elif state in SCANNING_STATES and ctx.next_stage is not None:
            self.next_chip.set_level("info", f"NEXT: {ctx.next_stage.label.upper()}")
        elif state == S.VALIDATION_PASSED:
            self.next_chip.set_level("warn", "WAITING")
        elif loading:
            self.next_chip.set_level("ok", "LOADING")
        elif state == S.LOADING_COMPLETED:
            self.next_chip.set_level("ok", "COMPLETE")
        else:
            self.next_chip.set_level("neutral", "SELECT BAG TYPE" if ctx.bag_type is None else "READY")

        self._render_stage_table(ctx)
        self._render_material(ctx)
        self._render_steps(ctx)
        self._render_readiness()
        self._render_status()
        if self.sim_card.isVisible() and ctx.bag_type and ctx.bag_type.id != self._sim_bag_type_id:
            self._rebuild_sim_choices()
        if not self._notice_timer.isActive():
            self._set_notice("", "neutral")

    def _render_stage_table(self, ctx: ProcessContext) -> None:
        items = [i for i in CheckItem if i != CheckItem.MOLD or Stage.MOLD in ctx.plan]
        table = self.stage_table
        table.setRowCount(len(items))
        current: set[CheckItem] = set()
        if ctx.state in SCANNING_STATES and ctx.next_stage is not None and not ctx.alarm_active:
            current.add(ctx.next_stage.check_item)
            if ctx.next_stage == Stage.BAG:
                current.add(CheckItem.COLOUR)
        for r, item in enumerate(items):
            row = ctx.rows.get(item)
            result = row.result if row else CheckResult.PENDING
            is_current = item in current
            scanned = row.scanned if row else ""
            if not scanned and is_current:
                scanned = "Scan now"
            values = [item.label, row.expected if row else "—", scanned, "", row.message if row else ""]
            if result == CheckResult.OK:
                result_text, (fg, bg) = "OK", LEVELS["ok"]
            elif result == CheckResult.NG:
                result_text, (fg, bg) = "NG", LEVELS["ng"]
            elif result == CheckResult.NA:
                result_text, (fg, bg) = "N/A", SOFT["neutral"]
            elif is_current:
                result_text, (fg, bg) = "SCAN", SOFT["info"]
            else:
                result_text, (fg, bg) = "—", SOFT["neutral"]
            values[3] = result_text
            row_bg = "#EFF6FF" if is_current else ("#FFF5F5" if result == CheckResult.NG else "#FFFFFF")
            for c, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
                if c == 3:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    cell.setBackground(QBrush(QColor(bg)))
                    cell.setForeground(QBrush(QColor(fg)))
                    cell.setFont(font(13, True))
                else:
                    cell.setBackground(QBrush(QColor(row_bg)))
                    if c == 0:
                        cell.setFont(font(12, True))
                    elif c == 4:
                        cell.setFont(font(10))
                        if result == CheckResult.NG:
                            cell.setForeground(QBrush(QColor("#B91C1C")))
                    elif c == 2 and is_current and not (row and row.scanned):
                        cell.setForeground(QBrush(QColor("#1D4ED8")))
                    if text and c in (1, 2, 4):
                        cell.setToolTip(text)
                table.setItem(r, c, cell)
        for item, chip in self.matrix_chips.items():
            row = ctx.rows.get(item)
            chip.set_result(row.result.value if row else "PENDING")
            if row is None or row.result == CheckResult.PENDING:
                chip.set_level("neutral", "—")
            show = item != CheckItem.MOLD or Stage.MOLD in ctx.plan
            chip.setVisible(show)
            self.matrix_captions[item].setVisible(show)
        overall = ctx.overall if ctx.bag_type else CheckResult.PENDING
        self.overall_chip.set_result(overall.value)
        if overall == CheckResult.PENDING:
            self.overall_chip.set_level("neutral", "PENDING")

    def _render_material(self, ctx: ProcessContext) -> None:
        g = self.material_grid
        bag_type = ctx.bag_type
        g.set("bag_type", bag_type.name if bag_type else "—")
        g.set("colour", bag_type.colour if bag_type else "—")
        g.set("material", bag_type.material_type if bag_type else "—")
        g.set("weight", f"+ {ctx.bag_weight_kg:g} kg" if bag_type else "—")
        g.set("hopper", ctx.hopper.name if ctx.hopper else "—")
        g.set("silo", ctx.silo.name if ctx.silo else "Not required")
        g.set("distribution", ctx.distribution_tank.name if ctx.distribution_tank else "Not required")
        g.set("destination", ctx.destination.name if ctx.destination else "—")
        completed = ctx.state == S.LOADING_COMPLETED
        g.set("existing", fmt_kg(ctx.existing_qty_kg))
        g.set("total", fmt_kg(ctx.total_qty_kg), "ok" if completed else None)
        capacity = ctx.destination.capacity_kg if ctx.destination else None
        self.level_bar.set_level(ctx.total_qty_kg, capacity)
        self.level_bar.setVisible(ctx.destination is not None)

    def _render_steps(self, ctx: ProcessContext) -> None:
        state = ctx.state
        done_stages = set(self.tm.sm.completed_stages)
        failed_stage = self.tm.sm.failed_stage
        after_validation = state in (S.VALIDATION_PASSED, S.LOADING_AUTHORIZED, S.LOADING_IN_PROGRESS,
                                     S.LOADING_COMPLETED)
        steps = [("Bag Type", "done" if ctx.bag_type else "current")]
        for stage in ctx.plan or [Stage.BAG, Stage.HOPPER]:
            if stage in done_stages or after_validation:
                status = "done"
            elif state == S.VALIDATION_FAILED and failed_stage == stage:
                status = "failed"
            elif state in SCANNING_STATES and ctx.next_stage == stage and ctx.bag_type:
                status = "current"
            else:
                status = "pending"
            steps.append((stage.label, status))
        steps.append(("Validation", "done" if after_validation else "pending"))
        steps.append(("Authorised", "done" if state in (S.LOADING_AUTHORIZED, S.LOADING_IN_PROGRESS,
                                                         S.LOADING_COMPLETED)
                      else "waiting" if state == S.VALIDATION_PASSED else "pending"))
        steps.append(("Loading", "done" if state == S.LOADING_COMPLETED else "current" if state in LOADING_STATES
                      else "pending"))
        steps.append(("Complete", "done" if state == S.LOADING_COMPLETED else "pending"))
        if state == S.CANCELLED:
            steps = [(title, "pending" if status != "done" else "done") for title, status in steps]
        self.steps.set_steps(steps)

    def _render_readiness(self) -> None:
        ctx = self.tm.ctx
        try:
            checks = ctx.readiness if ctx.state == S.VALIDATION_PASSED and ctx.readiness else self.tm.readiness_checks()
        except Exception:  # noqa: BLE001
            checks = []
        lines = []
        for check in checks:
            colour = "#16A34A" if check.ok else "#DC2626"
            lines.append(f"<span style='color:{colour}; font-size:13pt;'>●</span>&nbsp; <b>{check.label}</b>: "
                         f"{check.detail}")
        mode = CONFIRMATION_TEXT.get(ctx.confirmation_mode, ctx.confirmation_mode)
        lines.append(f"<span style='color:#5B6776;'>Completion: {mode}</span>")
        self.readiness_label.setText("<br>".join(lines))
        info = ctx.loading_info
        if ctx.state in LOADING_STATES and self.ctx.plc.simulated and not info:
            info = "PLC simulator active."
        self.loading_info.setText(info)
        self.loading_info.setVisible(bool(info))

    def _render_status(self) -> None:
        ctx = self.tm.ctx
        plc = self.ctx.plc
        g = self.status_grid
        ready, _detail = plc.is_ready()
        if ctx.alarm_active:
            g.set("system", "ALARM", "ng")
        elif ctx.state in LOADING_STATES:
            g.set("system", "LOADING", "ok")
        elif ctx.state == S.VALIDATION_PASSED:
            g.set("system", "WAITING", "warn")
        elif not ready:
            g.set("system", "NOT READY", "warn")
        else:
            g.set("system", "READY", "ok")
        if not plc.config.enabled:
            g.set("plc", "Disabled", "neutral")
        elif plc.connected:
            g.set("plc", "Simulator" if plc.simulated else "Connected", "info" if plc.simulated else "ok")
        else:
            g.set("plc", "Disconnected", "ng")
        g.set("heartbeat", *heartbeat_label(plc.heartbeat_state))
        level, text = self.ctx.scanner.status()
        g.set("scanner", text, level)
        level, text = self.ctx.mes.status()
        g.set("mes", text, level)
        counts = self.ctx.mes.counts or {}
        pending = counts.get("PENDING", 0) + counts.get("SYNCING", 0)
        failed = counts.get("FAILED", 0)
        g.set("pending", f"{pending}" + (f"  ({failed} failed)" if failed else ""),
              "ng" if failed else ("warn" if pending else "ok"))
        last = ctx.last_completed
        g.set("last", f"{last['transaction_no']} at {fmt_time(last['completed_at'])}" if last else "—")

    # ================================================================== simulation
    def _rebuild_sim_choices(self) -> None:
        self.sim_combo.clear()
        bag_type = self.tm.ctx.bag_type
        self._sim_bag_type_id = bag_type.id if bag_type else None
        try:
            bag_types = self.tm.bag_type_options()
            equipment = self.ctx.mapping.list_equipment(active_only=True)
        except Exception:  # noqa: BLE001
            return
        ordered = ([bag_type] if bag_type else []) + [b for b in bag_types if not bag_type or b.id != bag_type.id]
        for index, bt in enumerate(ordered):
            if bt.match_type in (BarcodeMatchType.PREFIX, BarcodeMatchType.EXACT, BarcodeMatchType.CONTAINS):
                tag = "selected type" if index == 0 and bag_type else "other type"
                self.sim_combo.addItem(f"Bag barcode – {bt.name} ({tag})", ("BAG", bt.id))
        for eq in equipment:
            self.sim_combo.addItem(f"{eq.category.label} – {eq.name}  [{eq.barcode}]", ("EQ", eq.barcode))

    def _sample_bag_barcode(self, bag_type_id: int) -> Optional[str]:
        bag_type = next((b for b in self.tm.bag_type_options() if b.id == bag_type_id), None)
        if bag_type is None:
            return None
        self._sim_counter += 1
        serial = f"{int(time.time()) % 100000:05d}{self._sim_counter:02d}"
        if bag_type.match_type == BarcodeMatchType.EXACT:
            return bag_type.match_value
        if bag_type.match_type == BarcodeMatchType.CONTAINS:
            return f"SIM-{bag_type.match_value}-{serial}"
        return f"{bag_type.match_value}{serial}"

    def _send_sim_choice(self) -> None:
        data = self.sim_combo.currentData()
        if not data:
            return
        kind, value = data
        barcode = self._sample_bag_barcode(value) if kind == "BAG" else value
        if barcode:
            self.ctx.scanner.submit_simulated(barcode)

    def _send_sim_text(self) -> None:
        text = self.sim_text.text().strip()
        if text:
            self.ctx.scanner.submit_simulated(text)
            self.sim_text.clear()
