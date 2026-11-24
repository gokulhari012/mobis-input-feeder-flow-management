"""Tank inventory: estimated quantities, history and audited corrections."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from mobis.core.inventory_manager import TankLevel
from mobis.core.permissions import Permission
from mobis.ui.base_page import BasePage
from mobis.ui.widgets.common import (
    Card,
    LevelBar,
    MessageBox,
    PageHeader,
    button,
    fmt_kg,
    label,
    show_error,
    show_info,
)
from mobis.ui.widgets.table_model import Column, RecordTableModel, make_table
from mobis.utils.timeutil import fmt_datetime

RIGHT = Qt.AlignmentFlag.AlignRight


def _kg(value, _row) -> str:
    return "" if value is None else f"{value:,.3f}".rstrip("0").rstrip(".")


HISTORY_COLUMNS = [
    Column("changed_at", "Date / Time", 150),
    Column("equipment_name", "Tank", 150),
    Column("change_type", "Type", 100,
           level=lambda v, r: {"LOADING": "ok", "CORRECTION": "warn", "INITIAL": "info"}.get(v)),
    Column("quantity_before_kg", "Before (kg)", 100, _kg, RIGHT),
    Column("change_kg", "Change (kg)", 100, lambda v, r: "" if v is None else f"{v:+,.3f}".rstrip("0").rstrip("."),
           RIGHT),
    Column("quantity_after_kg", "After (kg)", 100, _kg, RIGHT),
    Column("transaction_no", "Transaction", 170),
    Column("username", "User", 90),
    Column("reason", "Reason"),
]


class TankCard(QFrame):
    clicked = Signal(int)

    def __init__(self, level: TankLevel, parent=None):
        super().__init__(parent)
        self.equipment_id = level.equipment.id
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(label(level.equipment.name, "CardTitle"))
        top.addStretch(1)
        top.addWidget(label(level.equipment.code, "Muted"))
        layout.addLayout(top)
        layout.addWidget(label(fmt_kg(level.quantity_kg), "BigValue"))
        bar = LevelBar()
        bar.set_level(level.quantity_kg, level.capacity_kg)
        layout.addWidget(bar)
        details = f"Last change: {fmt_datetime(level.last_change_at) or '—'}"
        if level.last_correction_at:
            details += f"\nLast correction: {fmt_datetime(level.last_correction_at)}"
        layout.addWidget(label(details, "Hint", wrap=True))
        if level.over_capacity:
            layout.addWidget(label("Estimated quantity exceeds capacity - verify and correct.", "ErrorText", wrap=True))

    def set_selected(self, selected: bool) -> None:
        self.setStyleSheet("QFrame#Card { border: 2px solid #1D4ED8; }" if selected else "")

    def mousePressEvent(self, event):  # noqa: N802
        self.clicked.emit(self.equipment_id)
        super().mousePressEvent(event)


class CorrectionDialog(QDialog):
    def __init__(self, levels: list[TankLevel], selected_id: Optional[int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Correct Tank Quantity")
        self.setMinimumWidth(460)
        self._levels = {lvl.equipment.id: lvl for lvl in levels}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addWidget(label("Use after a physical stock count. The correction and its reason are recorded in the "
                               "inventory history and the audit log.", "Muted", wrap=True))
        form = QFormLayout()
        self.tank = QComboBox()
        for lvl in levels:
            self.tank.addItem(f"{lvl.equipment.name} ({lvl.equipment.code})", lvl.equipment.id)
        if selected_id is not None and self.tank.findData(selected_id) >= 0:
            self.tank.setCurrentIndex(self.tank.findData(selected_id))
        self.current = label("", "ValueLabel")
        self.quantity = QDoubleSpinBox()
        self.quantity.setRange(0, 10_000_000)
        self.quantity.setDecimals(3)
        self.quantity.setSuffix(" kg")
        self.quantity.setSingleStep(25)
        self.difference = label("", "Muted")
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("e.g. Physical stock count 24.11.2026")
        form.addRow(label("Tank", "FieldLabel"), self.tank)
        form.addRow(label("Current (estimated)", "FieldLabel"), self.current)
        form.addRow(label("Actual quantity", "FieldLabel"), self.quantity)
        form.addRow(label("Difference", "FieldLabel"), self.difference)
        form.addRow(label("Reason *", "FieldLabel"), self.reason)
        layout.addLayout(form)
        self.error = label("", "ErrorText", wrap=True)
        layout.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        ok = button("Save Correction", "primary")
        ok.clicked.connect(self._on_ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)
        self.tank.currentIndexChanged.connect(self._on_tank)
        self.quantity.valueChanged.connect(self._update_difference)
        self._on_tank()

    def _current_qty(self) -> float:
        lvl = self._levels.get(self.tank.currentData())
        return lvl.quantity_kg if lvl else 0.0

    def _on_tank(self) -> None:
        qty = self._current_qty()
        self.current.setText(fmt_kg(qty, 3))
        self.quantity.setValue(qty)
        self._update_difference()

    def _update_difference(self) -> None:
        diff = self.quantity.value() - self._current_qty()
        self.difference.setText(f"{diff:+,.3f} kg")

    def _on_ok(self) -> None:
        if not self.reason.text().strip():
            self.error.setText("A reason is required.")
            return
        self.accept()

    def values(self) -> tuple[int, float, str]:
        return self.tank.currentData(), self.quantity.value(), self.reason.text().strip()


class InventoryPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._selected: Optional[int] = None
        self._cards: list[TankCard] = []
        self._levels: list[TankLevel] = []

        header = PageHeader("Tank Inventory", "Estimated material quantity in each silo and distribution tank.")
        self.btn_correct = button("Correct Quantity…", "primary")
        self.btn_correct.clicked.connect(self._on_correct)
        refresh = button("Refresh")
        refresh.clicked.connect(self.refresh)
        header.add_action(refresh)
        header.add_action(self.btn_correct)
        self.root.addWidget(header)
        self.root.addWidget(MessageBox(
            "Updated quantity = existing quantity + bag weight, applied only when a loading is confirmed. "
            "Material consumption is not measured - correct the quantity after each physical stock count."))

        self.cards_host = QWidget()
        self.cards_grid = QGridLayout(self.cards_host)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.cards_grid.setSpacing(12)
        self.root.addWidget(self.cards_host)

        history = Card("Quantity History")
        self.history_filter = label("All tanks", "Muted")
        history.add_header_widget(self.history_filter)
        show_all = button("Show All Tanks")
        show_all.setProperty("flat", "true")
        show_all.clicked.connect(lambda: self._select(None))
        history.add_header_widget(show_all)
        self.model = RecordTableModel(HISTORY_COLUMNS)
        self.table, _proxy = make_table(self.model)
        history.body.addWidget(self.table)
        self.root.addWidget(history, 1)

        ctx.hub.inventory_changed.connect(self._refresh_if_visible)
        ctx.hub.equipment_changed.connect(self._refresh_if_visible)

    def on_show(self) -> None:
        self.btn_correct.setVisible(self.ctx.auth.has_permission(Permission.CORRECT_INVENTORY))
        self.refresh()

    def on_user_changed(self) -> None:
        self.btn_correct.setVisible(self.ctx.auth.has_permission(Permission.CORRECT_INVENTORY))

    def _refresh_if_visible(self) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        self._levels = self.ctx.inventory.levels()
        while self.cards_grid.count():
            item = self.cards_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards = []
        for index, level in enumerate(self._levels):
            card = TankCard(level)
            card.clicked.connect(self._select)
            card.set_selected(level.equipment.id == self._selected)
            self.cards_grid.addWidget(card, index // 4, index % 4)
            self._cards.append(card)
        self._load_history()

    def _select(self, equipment_id: Optional[int]) -> None:
        self._selected = equipment_id
        for card in self._cards:
            card.set_selected(card.equipment_id == equipment_id)
        self._load_history()

    def _load_history(self) -> None:
        rows = self.ctx.inventory.history(self._selected, limit=1000)
        self.model.set_rows(rows)
        name = next((lvl.equipment.name for lvl in self._levels if lvl.equipment.id == self._selected), None)
        self.history_filter.setText(name or "All tanks")

    def _on_correct(self) -> None:
        if not self.ctx.auth.has_permission(Permission.CORRECT_INVENTORY):
            return
        dialog = CorrectionDialog(self._levels, self._selected, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        equipment_id, quantity, reason = dialog.values()
        try:
            before, after = self.ctx.inventory.correct(equipment_id, quantity, reason, self.username)
        except ValueError as exc:
            show_error(self, "Correction", str(exc))
            return
        self.ctx.hub.inventory_changed.emit()
        self.refresh()
        show_info(self, "Correction saved", f"Quantity changed from {before:,.3f} kg to {after:,.3f} kg.")
