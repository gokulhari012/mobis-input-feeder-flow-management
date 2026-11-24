"""Create / edit a bag type and its equipment flow mapping."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
)

from mobis.core.barcode_parser import describe_rule
from mobis.core.mapping_service import MappingError, MappingService
from mobis.core.models import BagType, BarcodeMatchType, EquipmentCategory
from mobis.core.validation_engine import ValidationEngine
from mobis.ui.widgets.common import Card, button, label

H, S, D, M = (EquipmentCategory.HOPPER, EquipmentCategory.SILO, EquipmentCategory.DISTRIBUTION_TANK,
              EquipmentCategory.MOLD)


class BagTypeDialog(QDialog):
    def __init__(self, mapping: MappingService, bag_type: Optional[BagType], username: str,
                 mold_enabled: bool, parent=None):
        super().__init__(parent)
        self.mapping = mapping
        self.original = bag_type
        self.username = username
        self.saved: Optional[BagType] = None
        self.snapshot = mapping.load_snapshot()
        self.equipment = sorted(self.snapshot.equipment.values(), key=lambda e: (e.category.value, e.sort_order, e.name))
        self.setWindowTitle("Edit Bag Type" if bag_type else "Add Bag Type")
        self.setMinimumWidth(900)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)

        # ------------------------------------------------------------ material
        material = Card("Material")
        form = QFormLayout()
        self.code = QLineEdit()
        self.name = QLineEdit()
        self.material_type = QLineEdit()
        self.colour = QLineEdit()
        self.colour_code = QLineEdit()
        self.colour_code.setPlaceholderText("Colour as encoded in the barcode (optional)")
        self.weight = QDoubleSpinBox()
        self.weight.setRange(0, 100_000)
        self.weight.setDecimals(3)
        self.weight.setSuffix(" kg")
        self.description = QLineEdit()
        self.active = QCheckBox("Active (selectable by operators)")
        for caption, widget in (("Bag type code *", self.code), ("Bag type name *", self.name),
                                ("Material type *", self.material_type), ("Colour *", self.colour),
                                ("Colour code", self.colour_code), ("Bag weight *", self.weight),
                                ("Description", self.description), ("", self.active)):
            form.addRow(label(caption, "FieldLabel"), widget)
        material.body.addLayout(form)
        grid.addWidget(material, 0, 0)

        # ------------------------------------------------------------ identification
        ident = Card("Bag Barcode Identification")
        form = QFormLayout()
        self.match_type = QComboBox()
        for mt in BarcodeMatchType:
            self.match_type.addItem(mt.label, mt.value)
        self.match_value = QLineEdit()
        self.match_value.setPlaceholderText("e.g. prefix printed on the bag barcode")
        self.test_input = QLineEdit()
        self.test_input.setPlaceholderText("Scan or type a sample bag barcode…")
        self.test_result = label("", wrap=True)
        form.addRow(label("Rule *", "FieldLabel"), self.match_type)
        form.addRow(label("Value *", "FieldLabel"), self.match_value)
        form.addRow(label("Test barcode", "FieldLabel"), self.test_input)
        form.addRow(label("", "FieldLabel"), self.test_result)
        ident.body.addLayout(form)
        ident.body.addWidget(label(
            "The global bag barcode structure (delimited fields / regular expression) is configured on the "
            "Bag Type Mapping page. 'Material code field' rules require that structure.", "Hint", wrap=True))
        grid.addWidget(ident, 1, 0)

        # ------------------------------------------------------------ flow
        flow = Card("Equipment Flow")
        form = QFormLayout()
        self.hopper = QComboBox()
        self.routing_hint = label("", "Hint", wrap=True)
        self.silo = QComboBox()
        self.silo_scan = QCheckBox("Silo barcode must be scanned")
        self.tank = QComboBox()
        self.tank_scan = QCheckBox("Distribution tank barcode must be scanned")
        self.mold = QComboBox()
        self.mold_scan = QCheckBox("Mold / machine barcode must be scanned")
        self.show_all = QCheckBox("Show equipment not linked by routing")
        form.addRow(label("Hopper *", "FieldLabel"), self.hopper)
        form.addRow(label("", "FieldLabel"), self.routing_hint)
        form.addRow(label("Silo", "FieldLabel"), self.silo)
        form.addRow(label("", "FieldLabel"), self.silo_scan)
        form.addRow(label("Distribution tank", "FieldLabel"), self.tank)
        form.addRow(label("", "FieldLabel"), self.tank_scan)
        form.addRow(label("Mold / machine", "FieldLabel"), self.mold)
        form.addRow(label("", "FieldLabel"), self.mold_scan)
        form.addRow(label("", "FieldLabel"), self.show_all)
        flow.body.addLayout(form)
        note = ("Quantity is booked to the distribution tank when assigned, otherwise to the silo.")
        if not mold_enabled:
            note += " Mold / machine verification is currently disabled in Administration > Application."
        flow.body.addWidget(label(note, "Hint", wrap=True))
        grid.addWidget(flow, 0, 1, 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.errors = label("", "ErrorText", wrap=True)
        layout.addWidget(self.errors)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        save = button("Save Bag Type", "primary")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self._load(bag_type)
        self.hopper.currentIndexChanged.connect(self._refresh_targets)
        self.silo.currentIndexChanged.connect(self._on_silo)
        self.tank.currentIndexChanged.connect(self._on_tank)
        self.mold.currentIndexChanged.connect(self._sync_scan_boxes)
        self.show_all.toggled.connect(self._refresh_targets)
        for widget in (self.test_input, self.match_value, self.colour, self.colour_code):
            widget.textChanged.connect(self._run_test)
        self.match_type.currentIndexChanged.connect(self._run_test)

    # ------------------------------------------------------------------ loading
    def _load(self, bag_type: Optional[BagType]) -> None:
        bt = bag_type or BagType(0, "", "", "", "", weight_kg=25.0, is_active=True)
        self.code.setText(bt.code)
        self.name.setText(bt.name)
        self.material_type.setText(bt.material_type)
        self.colour.setText(bt.colour)
        self.colour_code.setText(bt.colour_code)
        self.weight.setValue(bt.weight_kg)
        self.description.setText(bt.description)
        self.active.setChecked(bt.is_active)
        self.match_type.setCurrentIndex(max(0, self.match_type.findData(bt.match_type.value)))
        self.match_value.setText(bt.match_value)
        self.hopper.addItem("— Select hopper —", None)
        for eq in self.equipment:
            if eq.category == H and (eq.is_active or eq.id == bt.hopper_id):
                self.hopper.addItem(eq.name + ("" if eq.is_active else " (inactive)"), eq.id)
        self.hopper.setCurrentIndex(max(0, self.hopper.findData(bt.hopper_id)))
        self._selected = {"silo": bt.silo_id, "tank": bt.distribution_tank_id, "mold": bt.mold_id}
        self.silo_scan.setChecked(bt.silo_scan_required)
        self.tank_scan.setChecked(bt.distribution_scan_required)
        self.mold_scan.setChecked(bt.mold_scan_required)
        if bag_type and (
            (bt.silo_id and not self.snapshot.is_linked(bt.hopper_id, bt.silo_id))
            or (bt.distribution_tank_id and not self.snapshot.is_linked(bt.hopper_id, bt.distribution_tank_id)
                and not self.snapshot.is_linked(bt.silo_id, bt.distribution_tank_id))
        ):
            self.show_all.setChecked(True)
        self._refresh_targets()

    def _fill(self, combo: QComboBox, none_text: str, items, selected) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(none_text, None)
        for eq in items:
            combo.addItem(eq.name + ("" if eq.is_active else " (inactive)"), eq.id)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _refresh_targets(self) -> None:
        hopper_id = self.hopper.currentData()
        show_all = self.show_all.isChecked()
        snap = self.snapshot

        def visible(eq, current_id):
            return eq.is_active or eq.id == current_id

        silos = [e for e in self.equipment if e.category == S and visible(e, self._selected["silo"])
                 and (show_all or snap.is_linked(hopper_id, e.id))]
        self._fill(self.silo, "— None —", silos, self._selected["silo"])
        silo_id = self.silo.currentData()
        tanks = [e for e in self.equipment if e.category == D and visible(e, self._selected["tank"])
                 and (show_all or snap.is_linked(hopper_id, e.id) or snap.is_linked(silo_id, e.id))]
        self._fill(self.tank, "— None —", tanks, self._selected["tank"])
        molds = [e for e in self.equipment if e.category == M and visible(e, self._selected["mold"])
                 and (show_all or snap.is_linked(hopper_id, e.id))]
        self._fill(self.mold, "— Any mold fed by the hopper —", molds, self._selected["mold"])
        targets = snap.targets_of(hopper_id)
        self.routing_hint.setText(
            ("Feeds: " + ", ".join(t.name for t in targets)) if targets else
            ("No routing links defined for this hopper (Equipment > Routing)." if hopper_id else "")
        )
        self._sync_scan_boxes()

    def _on_silo(self) -> None:
        newly_assigned = self._selected["silo"] is None and self.silo.currentData() is not None
        self._selected["silo"] = self.silo.currentData()
        self._refresh_targets()
        if newly_assigned:  # default: a newly assigned silo must be scanned
            self.silo_scan.setChecked(True)

    def _on_tank(self) -> None:
        newly_assigned = self._selected["tank"] is None and self.tank.currentData() is not None
        self._selected["tank"] = self.tank.currentData()
        self._sync_scan_boxes()
        if newly_assigned:
            self.tank_scan.setChecked(True)

    def _sync_scan_boxes(self) -> None:
        self._selected["mold"] = self.mold.currentData()
        has_silo = self.silo.currentData() is not None
        has_tank = self.tank.currentData() is not None
        for box, enabled in ((self.silo_scan, has_silo), (self.tank_scan, has_tank)):
            if not enabled:
                box.setChecked(False)
            box.setEnabled(enabled)

    # ------------------------------------------------------------------ test / save
    def _candidate(self) -> BagType:
        return BagType(
            id=self.original.id if self.original else 0,
            code=self.code.text().strip(),
            name=self.name.text().strip(),
            material_type=self.material_type.text().strip(),
            colour=self.colour.text().strip(),
            colour_code=self.colour_code.text().strip(),
            match_type=BarcodeMatchType(self.match_type.currentData()),
            match_value=self.match_value.text().strip(),
            weight_kg=self.weight.value(),
            hopper_id=self.hopper.currentData(),
            silo_id=self.silo.currentData(),
            silo_scan_required=self.silo_scan.isChecked(),
            distribution_tank_id=self.tank.currentData(),
            distribution_scan_required=self.tank_scan.isChecked(),
            mold_id=self.mold.currentData(),
            mold_scan_required=self.mold_scan.isChecked(),
            description=self.description.text().strip(),
            is_active=self.active.isChecked(),
        )

    def _run_test(self) -> None:
        text = self.test_input.text().strip()
        if not text:
            self.test_result.setText("")
            return
        candidate = self._candidate()
        if not candidate.match_value:
            self.test_result.setText("Enter the identification value first.")
            return
        bag_types = dict(self.snapshot.bag_types)
        bag_types[candidate.id or -1] = replace(candidate, id=candidate.id or -1)
        engine = ValidationEngine(replace(self.snapshot, bag_types=bag_types))
        check = engine.validate_bag(bag_types[candidate.id or -1], text)
        if check.ok:
            parsed = check.parsed
            extra = ", ".join(f"{k}: {v}" for k, v in (("lot", parsed.lot), ("serial", parsed.serial),
                                                         ("weight", parsed.weight_kg)) if v)
            self.test_result.setText(f"<span style='color:#15803D'><b>OK</b> – matches ({describe_rule(candidate)})"
                                     f"{'; ' + extra if extra else ''}</span>")
        else:
            self.test_result.setText(f"<span style='color:#B91C1C'><b>NG</b> – {check.failure.message}</span>")

    def _save(self) -> None:
        candidate = self._candidate()
        try:
            self.saved = self.mapping.save_bag_type(candidate, self.username)
        except MappingError as exc:
            self.errors.setText(str(exc))
            return
        self.accept()
