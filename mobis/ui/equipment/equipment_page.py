"""Equipment master data (hoppers, silos, distribution tanks, molds) and routing links."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mobis.core.mapping_service import MappingError
from mobis.core.models import STORAGE_CATEGORIES, Equipment, EquipmentCategory
from mobis.hardware.plc_manager import address_problem
from mobis.ui.base_page import BasePage
from mobis.ui.widgets.common import (
    Card,
    PageHeader,
    button,
    confirm,
    label,
    repolish,
    show_error,
)
from mobis.ui.widgets.table_model import Column, RecordTableModel, make_table, selected_row

EQUIPMENT_COLUMNS = [
    Column("code", "Equipment ID", 125),
    Column("name", "Name", 160),
    Column("category", "Category", 150),
    Column("barcode", "Barcode Value", 150),
    Column("plc_code", "PLC Code", 75, align=Qt.AlignmentFlag.AlignRight),
    Column("ready_signal", "Limit Switch Signal", 140),
    Column("capacity_kg", "Capacity (kg)", 100, lambda v, r: "" if v is None else f"{v:,.0f}",
           Qt.AlignmentFlag.AlignRight),
    Column("active", "Active", 60, lambda v, r: "Yes" if v else "No", level=lambda v, r: None if v else "neutral"),
    Column("used_by", "Used by Bag Types", 150),
    Column("description", "Description"),
]

LINK_COLUMNS = [
    Column("source", "Source (feeds)", 180),
    Column("source_category", "Source Type", 140),
    Column("target", "Target (receives)", 180),
    Column("target_category", "Target Type", 160),
    Column("active", "Active", 60, lambda v, r: "Yes" if v else "No", level=lambda v, r: None if v else "neutral"),
    Column("description", "Description"),
]


class EquipmentDialog(QDialog):
    def __init__(self, ctx, equipment: Optional[Equipment], parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.original = equipment
        self.setWindowTitle("Edit Equipment" if equipment else "Add Equipment")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        self.code = QLineEdit()
        self.name = QLineEdit()
        self.category = QComboBox()
        for cat in EquipmentCategory:
            self.category.addItem(cat.label, cat.value)
        self.barcode = QLineEdit()
        self.barcode.setPlaceholderText("Value printed on the equipment barcode label")
        self.description = QLineEdit()
        self.capacity = QDoubleSpinBox()
        self.capacity.setRange(0, 1_000_000)
        self.capacity.setDecimals(0)
        self.capacity.setSuffix(" kg")
        self.initial_qty = QDoubleSpinBox()
        self.initial_qty.setRange(0, 1_000_000)
        self.initial_qty.setDecimals(3)
        self.initial_qty.setSuffix(" kg")
        self.plc_code_set = QCheckBox("Assigned")
        self.plc_code = QSpinBox()
        self.plc_code.setRange(0, 32767)
        plc_row = QHBoxLayout()
        plc_row.addWidget(self.plc_code_set)
        plc_row.addWidget(self.plc_code, 1)
        self.ready_address = QLineEdit()
        self.ready_address.setPlaceholderText("e.g. X20 (optional)")
        self.ready_ok = QSpinBox()
        self.ready_ok.setRange(0, 65535)
        self.sort_order = QSpinBox()
        self.sort_order.setRange(0, 9999)
        self.active = QCheckBox("Active")
        rows = [
            ("Equipment ID *", self.code), ("Name *", self.name), ("Category *", self.category),
            ("Barcode value *", self.barcode), ("Description", self.description),
            ("Capacity", self.capacity), ("Initial quantity", self.initial_qty),
        ]
        for caption, widget in rows:
            form.addRow(label(caption, "FieldLabel"), widget)
        form.addRow(label("PLC code", "FieldLabel"), plc_row)
        form.addRow(label("Limit switch / ready signal", "FieldLabel"), self.ready_address)
        form.addRow(label("Ready when value =", "FieldLabel"), self.ready_ok)
        form.addRow(label("Sort order", "FieldLabel"), self.sort_order)
        form.addRow(label("", "FieldLabel"), self.active)
        layout.addLayout(form)
        layout.addWidget(label(
            "PLC code: number written to the PLC hopper / destination code registers when loading is authorised. "
            "Limit switch / ready signal: PLC device that must equal the configured value before loading is "
            "authorised (e.g. silo / tank limit switch). Both must be agreed with the PLC programmer.",
            "Hint", wrap=True))
        self.errors = label("", "ErrorText", wrap=True)
        layout.addWidget(self.errors)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        save = button("Save", "primary")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        eq = equipment
        self.code.setText(eq.code if eq else "")
        self.name.setText(eq.name if eq else "")
        self.category.setCurrentIndex(max(0, self.category.findData(eq.category.value if eq else "HOPPER")))
        self.barcode.setText(eq.barcode if eq else "")
        self.description.setText(eq.description if eq else "")
        self.capacity.setValue(eq.capacity_kg or 0 if eq else 0)
        self.plc_code_set.setChecked(bool(eq and eq.plc_code is not None))
        self.plc_code.setValue(eq.plc_code or 0 if eq else 0)
        self.ready_address.setText(eq.ready_signal_address if eq else "")
        self.ready_ok.setValue(eq.ready_signal_ok_value if eq else 1)
        self.sort_order.setValue(eq.sort_order if eq else 0)
        self.active.setChecked(eq.is_active if eq else True)
        self.category.currentIndexChanged.connect(self._sync)
        self.plc_code_set.toggled.connect(self._sync)
        self.ready_address.textChanged.connect(self._validate_address)
        self._sync()
        self._validate_address()

    def _series(self) -> str:
        return self.ctx.config.plc().plc_series

    def _validate_address(self) -> str:
        text = self.ready_address.text().strip()
        problem = address_problem(text, self._series()) if text else ""
        self.ready_address.setProperty("invalid", "true" if problem else "false")
        self.ready_address.setToolTip(problem)
        repolish(self.ready_address)
        self.ready_ok.setEnabled(bool(text))
        return problem

    def _sync(self) -> None:
        storage = EquipmentCategory(self.category.currentData()) in STORAGE_CATEGORIES
        self.capacity.setEnabled(storage)
        self.initial_qty.setEnabled(storage and self.original is None)
        self.plc_code.setEnabled(self.plc_code_set.isChecked())

    def _save(self) -> None:
        category = self.category.currentData()
        data = {
            "equipment_code": self.code.text().strip(),
            "name": self.name.text().strip(),
            "category": category,
            "barcode": self.barcode.text().strip(),
            "description": self.description.text().strip(),
            "capacity_kg": self.capacity.value() if EquipmentCategory(category) in STORAGE_CATEGORIES
            and self.capacity.value() > 0 else None,
            "plc_code": self.plc_code.value() if self.plc_code_set.isChecked() else None,
            "ready_signal_address": self.ready_address.text().strip().upper(),
            "ready_signal_ok_value": self.ready_ok.value(),
            "sort_order": self.sort_order.value(),
            "is_active": self.active.isChecked(),
        }
        others = self.ctx.mapping.barcode_used_in_other_categories(
            data["barcode"], category, self.original.id if self.original else None
        ) if data["barcode"] else []
        if others and not confirm(
            self, "Barcode Already Used",
            f"Barcode '{data['barcode']}' is also used by {', '.join(o.name for o in others)} (other category).\n\n"
            "This is allowed, but unique barcodes across all equipment are recommended. Save anyway?",
        ):
            return
        try:
            self.ctx.mapping.save_equipment(
                data, self.original.id if self.original else None, self.ctx.auth.audit_username(),
                initial_quantity_kg=self.initial_qty.value(),
                address_validator=lambda a: address_problem(a, self._series()),
            )
        except MappingError as exc:
            self.errors.setText(str(exc))
            return
        self.accept()


class LinkDialog(QDialog):
    ALLOWED = {
        EquipmentCategory.HOPPER: (EquipmentCategory.SILO, EquipmentCategory.DISTRIBUTION_TANK, EquipmentCategory.MOLD),
        EquipmentCategory.SILO: (EquipmentCategory.DISTRIBUTION_TANK, EquipmentCategory.MOLD),
        EquipmentCategory.DISTRIBUTION_TANK: (EquipmentCategory.MOLD,),
    }

    def __init__(self, equipment: list[Equipment], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Routing Link")
        self.setMinimumWidth(460)
        self._equipment = [e for e in equipment if e.is_active]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        self.source = QComboBox()
        for eq in self._equipment:
            if eq.category in self.ALLOWED:
                self.source.addItem(f"{eq.name} ({eq.category.label})", eq.id)
        self.target = QComboBox()
        self.description = QLineEdit()
        form.addRow(label("Source (feeds material)", "FieldLabel"), self.source)
        form.addRow(label("Target (receives material)", "FieldLabel"), self.target)
        form.addRow(label("Description", "FieldLabel"), self.description)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        ok = button("Add Link", "primary")
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
        self.source.currentIndexChanged.connect(self._fill_targets)
        self._fill_targets()

    def _fill_targets(self) -> None:
        self.target.clear()
        source = next((e for e in self._equipment if e.id == self.source.currentData()), None)
        if source is None:
            return
        for eq in self._equipment:
            if eq.category in self.ALLOWED.get(source.category, ()):
                self.target.addItem(f"{eq.name} ({eq.category.label})", eq.id)

    def values(self) -> tuple[Optional[int], Optional[int], str]:
        return self.source.currentData(), self.target.currentData(), self.description.text().strip()


class EquipmentPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.root.addWidget(PageHeader(
            "Equipment Configuration",
            "Hoppers, silos, distribution tanks and molds / machines, and the material routing between them."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)

        # ------------------------------------------------------------ equipment tab
        eq_tab = QWidget()
        eq_layout = QVBoxLayout(eq_tab)
        eq_layout.setContentsMargins(12, 12, 12, 12)
        bar = QHBoxLayout()
        bar.addWidget(label("Category", "FieldLabel"))
        self.category_filter = QComboBox()
        self.category_filter.addItem("All", "")
        for cat in EquipmentCategory:
            self.category_filter.addItem(cat.label, cat.value)
        self.category_filter.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.category_filter)
        self.show_inactive = QCheckBox("Show inactive")
        self.show_inactive.setChecked(True)
        self.show_inactive.toggled.connect(self.refresh)
        bar.addWidget(self.show_inactive)
        bar.addStretch(1)
        self.btn_add = button("Add Equipment", "primary")
        self.btn_edit = button("Edit")
        self.btn_toggle = button("Deactivate")
        for btn in (self.btn_add, self.btn_edit, self.btn_toggle):
            bar.addWidget(btn)
        self.btn_add.clicked.connect(lambda: self._open(None))
        self.btn_edit.clicked.connect(self._edit)
        self.btn_toggle.clicked.connect(self._toggle)
        eq_layout.addLayout(bar)
        self.eq_model = RecordTableModel(EQUIPMENT_COLUMNS)
        self.eq_table, _p = make_table(self.eq_model)
        self.eq_table.doubleClicked.connect(lambda _i: self._edit())
        self.eq_table.selectionModel().selectionChanged.connect(self._update_buttons)
        eq_layout.addWidget(self.eq_table, 1)
        self.tabs.addTab(eq_tab, "Equipment Master")

        # ------------------------------------------------------------ routing tab
        link_tab = QWidget()
        link_layout = QHBoxLayout(link_tab)
        link_layout.setContentsMargins(12, 12, 12, 12)
        left = QVBoxLayout()
        bar = QHBoxLayout()
        bar.addWidget(label("A bag type can only be mapped to a silo / tank that its hopper feeds.", "Muted"))
        bar.addStretch(1)
        self.btn_link_add = button("Add Link", "primary")
        self.btn_link_toggle = button("Activate / Deactivate")
        self.btn_link_delete = button("Delete", "danger")
        for btn in (self.btn_link_add, self.btn_link_toggle, self.btn_link_delete):
            bar.addWidget(btn)
        self.btn_link_add.clicked.connect(self._add_link)
        self.btn_link_toggle.clicked.connect(self._toggle_link)
        self.btn_link_delete.clicked.connect(self._delete_link)
        left.addLayout(bar)
        self.link_model = RecordTableModel(LINK_COLUMNS)
        self.link_table, _p = make_table(self.link_model)
        left.addWidget(self.link_table, 1)
        link_layout.addLayout(left, 3)
        summary = Card("Material Flow Summary")
        self.flow_summary = label("", wrap=True)
        self.flow_summary.setTextFormat(Qt.TextFormat.RichText)
        self.flow_summary.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        summary.body.addWidget(self.flow_summary, 1)
        link_layout.addWidget(summary, 1)
        self.tabs.addTab(link_tab, "Routing (Material Flow)")
        self._equipment: dict[int, Equipment] = {}

    def on_show(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ equipment
    def refresh(self) -> None:
        mapping = self.ctx.mapping
        all_equipment = mapping.list_equipment()
        self._equipment = {e.id: e for e in all_equipment}
        bag_types = mapping.list_bag_types()
        category = self.category_filter.currentData()
        rows = []
        for eq in all_equipment:
            if category and eq.category.value != category:
                continue
            if not eq.is_active and not self.show_inactive.isChecked():
                continue
            users = [b.code for b in bag_types if eq.id in (b.hopper_id, b.silo_id, b.distribution_tank_id, b.mold_id)]
            ready = f"{eq.ready_signal_address} = {eq.ready_signal_ok_value}" if eq.ready_signal_address else ""
            rows.append({
                "id": eq.id, "code": eq.code, "name": eq.name, "category": eq.category.label,
                "barcode": eq.barcode, "plc_code": eq.plc_code, "ready_signal": ready,
                "capacity_kg": eq.capacity_kg, "active": eq.is_active, "used_by": ", ".join(users),
                "description": eq.description,
            })
        self.eq_model.set_rows(rows)
        self._refresh_links()
        self._update_buttons()

    def _selected_equipment(self) -> Optional[Equipment]:
        row = selected_row(self.eq_table)
        return self._equipment.get(row["id"]) if row else None

    def _update_buttons(self) -> None:
        eq = self._selected_equipment()
        self.btn_edit.setEnabled(eq is not None)
        self.btn_toggle.setEnabled(eq is not None)
        self.btn_toggle.setText("Activate" if eq and not eq.is_active else "Deactivate")

    def _open(self, equipment: Optional[Equipment]) -> None:
        if EquipmentDialog(self.ctx, equipment, self).exec() == QDialog.DialogCode.Accepted:
            self.ctx.hub.equipment_changed.emit()
            self.ctx.hub.inventory_changed.emit()
            self.refresh()

    def _edit(self) -> None:
        eq = self._selected_equipment()
        if eq is not None:
            self._open(eq)

    def _toggle(self) -> None:
        eq = self._selected_equipment()
        if eq is None:
            return
        activate = not eq.is_active
        if not activate:
            users = self.ctx.mapping.bag_types_using(eq.id)
            text = f"Deactivate {eq.name}? Its barcode will be rejected during scanning."
            if users:
                text += f"\n\nThe following active bag types use it and will become unusable: {', '.join(users)}"
            if not confirm(self, "Deactivate Equipment", text, danger=bool(users), yes_text="Deactivate"):
                return
        self.ctx.mapping.set_equipment_active(eq.id, activate, self.username)
        self.ctx.hub.equipment_changed.emit()
        self.refresh()

    # ------------------------------------------------------------------ routing
    def _refresh_links(self) -> None:
        eq = self._equipment
        rows = []
        flows: dict[int, list[str]] = {}
        for link in self.ctx.mapping.list_links():
            src, tgt = eq.get(link.source_id), eq.get(link.target_id)
            if src is None or tgt is None:
                continue
            rows.append({
                "id": link.id, "source": src.name, "source_category": src.category.label, "target": tgt.name,
                "target_category": tgt.category.label, "active": link.is_active, "description": link.description,
            })
            if link.is_active:
                flows.setdefault(src.id, []).append(tgt.name)
        self.link_model.set_rows(rows)
        lines = []
        for src_id, targets in sorted(flows.items(), key=lambda kv: (eq[kv[0]].category.value, eq[kv[0]].name)):
            lines.append(f"<b>{eq[src_id].name}</b> &rarr; {', '.join(targets)}")
        unlinked = [e.name for e in eq.values() if e.category == EquipmentCategory.HOPPER and e.is_active
                    and e.id not in flows]
        if unlinked:
            lines.append(f"<br><span style='color:#C2410C'>Hoppers without routing: {', '.join(unlinked)}</span>")
        self.flow_summary.setText("<br>".join(lines) or "No routing links defined.")

    def _add_link(self) -> None:
        dialog = LinkDialog(list(self._equipment.values()), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        source, target, description = dialog.values()
        if source is None or target is None:
            return
        try:
            self.ctx.mapping.add_link(source, target, description, self.username)
        except MappingError as exc:
            show_error(self, "Add Routing Link", str(exc))
            return
        self.ctx.hub.equipment_changed.emit()
        self.refresh()

    def _toggle_link(self) -> None:
        row = selected_row(self.link_table)
        if row is None:
            return
        self.ctx.mapping.set_link_active(row["id"], not row["active"], self.username)
        self.ctx.hub.equipment_changed.emit()
        self.refresh()

    def _delete_link(self) -> None:
        row = selected_row(self.link_table)
        if row is None:
            return
        if not confirm(self, "Delete Routing Link",
                       f"Delete routing {row['source']} → {row['target']}?\n\nBag types mapped to this route will "
                       "report a configuration error until the mapping is corrected.", danger=True,
                       yes_text="Delete"):
            return
        self.ctx.mapping.delete_link(row["id"], self.username)
        self.ctx.hub.equipment_changed.emit()
        self.refresh()
