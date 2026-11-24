"""Bag types and their equipment mapping, plus the global bag barcode structure."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLineEdit, QSplitter, QVBoxLayout, QWidget

from mobis.core.barcode_parser import BagBarcodeParser, describe_rule
from mobis.core.mapping_service import MappingError
from mobis.core.models import BagType
from mobis.core.validation_engine import ValidationEngine
from mobis.ui.base_page import BasePage
from mobis.ui.widgets.common import Card, MessageBox, PageHeader, button, confirm, label, show_error, show_info
from mobis.ui.widgets.config_form import ConfigForm, FieldSpec
from mobis.ui.widgets.table_model import Column, RecordTableModel, make_table, selected_row

from .bag_type_dialog import BagTypeDialog

COLUMNS = [
    Column("code", "Code", 100),
    Column("name", "Bag Type", 150),
    Column("material_type", "Material", 80),
    Column("colour", "Colour", 80),
    Column("rule", "Barcode Rule", 170),
    Column("hopper", "Hopper", 95),
    Column("silo", "Silo", 95),
    Column("tank", "Distribution Tank", 135),
    Column("weight_kg", "Weight (kg)", 85, lambda v, r: f"{v:g}", Qt.AlignmentFlag.AlignRight),
    Column("scans", "Scans", 170),
    Column("active", "Active", 60, lambda v, r: "Yes" if v else "No",
           level=lambda v, r: None if v else "neutral"),
    Column("config", "Configuration", level=lambda v, r: "ok" if v == "OK" else "ng"),
]

FORMAT_SECTIONS = [
    ("Bag Barcode Structure", [
        FieldSpec("mode", "Structure", "choice", [("NONE", "Opaque (identify by rule only)"),
                                                  ("DELIMITED", "Delimited fields"),
                                                  ("REGEX", "Regular expression")],
                  tooltip="How the bag barcode is decoded. Real format to be confirmed with the material supplier."),
        FieldSpec("delimiter", "Delimiter", placeholder="| ; , or <GS>"),
        FieldSpec("field_material", "Material field #", "int", maximum=50, hint="1-based position, 0 = not present"),
        FieldSpec("field_colour", "Colour field #", "int", maximum=50),
        FieldSpec("field_lot", "Lot field #", "int", maximum=50),
        FieldSpec("field_serial", "Serial field #", "int", maximum=50),
        FieldSpec("field_weight", "Weight field #", "int", maximum=50),
        FieldSpec("regex", "Regular expression", placeholder="(?P<material>…)-(?P<colour>…)-(?P<serial>…)",
                  hint="Named groups: material, colour, lot, serial, weight"),
    ]),
    ("Rules", [
        FieldSpec("use_barcode_weight", "Use weight from barcode", "bool",
                  tooltip="Use the weight field of the barcode instead of the configured bag weight"),
        FieldSpec("prevent_duplicate_bag", "Reject already loaded bags", "bool",
                  tooltip="A bag barcode used in a completed loading is rejected (requires unique bag barcodes)"),
    ]),
]


class BagMappingPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        header = PageHeader("Bag Type & Flow Mapping",
                            "Material types, colours, barcode identification rules and their equipment flow.")
        self.btn_add = button("Add Bag Type", "primary")
        self.btn_edit = button("Edit")
        self.btn_toggle = button("Deactivate")
        self.btn_delete = button("Delete", "danger")
        for btn in (self.btn_add, self.btn_edit, self.btn_toggle, self.btn_delete):
            header.add_action(btn)
        self.btn_add.clicked.connect(self._add)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_toggle.clicked.connect(self._toggle)
        self.btn_delete.clicked.connect(self._delete)
        self.root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.show_inactive = QCheckBox("Show inactive bag types")
        self.show_inactive.toggled.connect(self.refresh)
        top_layout.addWidget(self.show_inactive)
        self.model = RecordTableModel(COLUMNS)
        self.table, _proxy = make_table(self.model)
        self.table.doubleClicked.connect(lambda _i: self._edit())
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        top_layout.addWidget(self.table, 1)
        splitter.addWidget(top)

        fmt_card = Card("Bag Barcode Format")
        fmt_card.body.addWidget(MessageBox(
            "The bag barcode data format has not been specified yet. Configure the structure here once the "
            "supplier label specification is available; until then bag types are identified by their rule."))
        row = QHBoxLayout()
        self.format_form = ConfigForm(FORMAT_SECTIONS, columns=2)
        row.addWidget(self.format_form, 3)
        test_box = QVBoxLayout()
        test_box.addWidget(label("Test a bag barcode", "SectionLabel"))
        self.test_input = QLineEdit()
        self.test_input.setPlaceholderText("Scan or type a bag barcode…")
        self.test_input.textChanged.connect(self._test_barcode)
        test_box.addWidget(self.test_input)
        self.test_output = label("", wrap=True)
        self.test_output.setTextFormat(Qt.TextFormat.RichText)
        test_box.addWidget(self.test_output)
        test_box.addStretch(1)
        self.btn_save_format = button("Save Barcode Format", "primary")
        self.btn_save_format.clicked.connect(self._save_format)
        test_box.addWidget(self.btn_save_format, 0, Qt.AlignmentFlag.AlignRight)
        row.addLayout(test_box, 2)
        fmt_card.body.addLayout(row)
        self.format_form.changed.connect(self._test_barcode)
        splitter.addWidget(fmt_card)
        splitter.setSizes([420, 380])
        self.root.addWidget(splitter, 1)
        self._bag_types: dict[int, BagType] = {}

    # ------------------------------------------------------------------ page
    def on_show(self) -> None:
        self.format_form.load_dataclass(self.ctx.config.barcode())
        self.refresh()

    def can_leave(self) -> bool:
        if self.format_form.is_dirty():
            return confirm(self, "Unsaved Changes", "Discard the unsaved barcode format changes?",
                           yes_text="Discard")
        return True

    def refresh(self) -> None:
        snapshot = self.ctx.mapping.load_snapshot()
        engine = ValidationEngine(snapshot)
        rows = []
        self._bag_types = {}
        for bt in sorted(snapshot.bag_types.values(), key=lambda b: b.name.casefold()):
            if not bt.is_active and not self.show_inactive.isChecked():
                continue
            self._bag_types[bt.id] = bt
            errors = engine.check_bag_type(bt)
            name = lambda eid: snapshot.get(eid).name if snapshot.get(eid) else ""  # noqa: E731
            rows.append({
                "id": bt.id, "code": bt.code, "name": bt.name, "material_type": bt.material_type,
                "colour": bt.colour, "rule": describe_rule(bt), "hopper": name(bt.hopper_id),
                "silo": name(bt.silo_id), "tank": name(bt.distribution_tank_id), "weight_kg": bt.weight_kg,
                "scans": ", ".join(s.label for s in engine.stage_plan(bt)),
                "active": bt.is_active, "config": "OK" if not errors else f"{len(errors)} issue(s): {errors[0]}",
            })
        self.model.set_rows(rows)
        self._update_buttons()

    def _selected(self) -> BagType | None:
        row = selected_row(self.table)
        return self._bag_types.get(row["id"]) if row else None

    def _update_buttons(self) -> None:
        bt = self._selected()
        for btn in (self.btn_edit, self.btn_toggle, self.btn_delete):
            btn.setEnabled(bt is not None)
        self.btn_toggle.setText("Activate" if bt and not bt.is_active else "Deactivate")

    # ------------------------------------------------------------------ actions
    def _open_dialog(self, bag_type: BagType | None) -> None:
        dialog = BagTypeDialog(self.ctx.mapping, bag_type, self.username,
                               self.ctx.config.app().mold_verification_enabled, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.ctx.hub.bag_types_changed.emit()
            self.refresh()

    def _add(self) -> None:
        self._open_dialog(None)

    def _edit(self) -> None:
        bt = self._selected()
        if bt is not None:
            self._open_dialog(bt)

    def _toggle(self) -> None:
        bt = self._selected()
        if bt is None:
            return
        activate = not bt.is_active
        if not activate and not confirm(self, "Deactivate Bag Type",
                                        f"Deactivate '{bt.name}'? Operators will no longer be able to select it."):
            return
        self.ctx.mapping.set_bag_type_active(bt.id, activate, self.username)
        self.ctx.hub.bag_types_changed.emit()
        self.refresh()

    def _delete(self) -> None:
        bt = self._selected()
        if bt is None:
            return
        usage = self.ctx.mapping.bag_type_usage(bt.id)
        if usage:
            show_info(self, "Delete Bag Type",
                      f"'{bt.name}' is referenced by {usage} transaction(s) and cannot be deleted "
                      "(traceability). Deactivate it instead.")
            return
        if not confirm(self, "Delete Bag Type", f"Permanently delete bag type '{bt.name}'?", danger=True,
                       yes_text="Delete"):
            return
        try:
            self.ctx.mapping.delete_bag_type(bt.id, self.username)
        except MappingError as exc:
            show_error(self, "Delete Bag Type", str(exc))
            return
        self.ctx.hub.bag_types_changed.emit()
        self.refresh()

    # ------------------------------------------------------------------ barcode format
    def _test_barcode(self) -> None:
        text = self.test_input.text().strip()
        if not text:
            self.test_output.setText("")
            return
        fmt = self.format_form.apply_to(self.ctx.config.barcode())
        parsed = BagBarcodeParser(fmt).parse(text)
        if not parsed.format_ok:
            self.test_output.setText(f"<span style='color:#B91C1C'><b>Format not recognised:</b> "
                                     f"{parsed.format_error}</span>")
            return
        snapshot = self.ctx.mapping.load_snapshot()
        engine = ValidationEngine(replace(snapshot, barcode_format=fmt))
        matches = engine.identify_bag_types(text)
        fields = [(k, v) for k, v in (("Material", parsed.material_code), ("Colour", parsed.colour),
                                      ("Lot", parsed.lot), ("Serial", parsed.serial),
                                      ("Weight", parsed.weight_kg)) if v not in ("", None)]
        lines = [f"<b>{k}:</b> {v}" for k, v in fields] or ["<i>No fields extracted (opaque barcode)</i>"]
        if matches:
            lines.append("<span style='color:#15803D'><b>Matches bag type:</b> "
                         + ", ".join(m.name for m in matches) + "</span>")
        else:
            lines.append("<span style='color:#B91C1C'><b>No active bag type matches this barcode.</b></span>")
        self.test_output.setText("<br>".join(lines))

    def _save_format(self) -> None:
        fmt = self.format_form.apply_to(self.ctx.config.barcode())
        parser = BagBarcodeParser(fmt)
        if parser.error:
            show_error(self, "Barcode Format", parser.error)
            return
        changed = self.ctx.config.save("barcode", fmt, self.username)
        self.format_form.mark_clean()
        self.refresh()
        show_info(self, "Barcode Format", "Saved." if changed else "No changes.")
