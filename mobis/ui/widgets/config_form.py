"""Declarative configuration form bound to a dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .common import Card, label, repolish


@dataclass
class FieldSpec:
    key: str
    label: str
    kind: str = "text"  # text | password | int | hexint | float | bool | choice | combo_edit | multiline | address
    choices: list[tuple[Any, str]] = field(default_factory=list)
    minimum: float = 0
    maximum: float = 1_000_000
    decimals: int = 2
    step: float = 1
    suffix: str = ""
    tooltip: str = ""
    placeholder: str = ""
    hint: str = ""


Section = tuple[str, list[FieldSpec]]


class ConfigForm(QWidget):
    changed = Signal()

    def __init__(self, sections: list[Section], address_validator: Optional[Callable[[str], str]] = None,
                 columns: int = 1, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._specs: dict[str, FieldSpec] = {}
        self._widgets: dict[str, QWidget] = {}
        self._address_validator = address_validator
        self._loaded: dict[str, Any] = {}
        self._loading = False
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for index, (title, specs) in enumerate(sections):
            card = Card(title)
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            form.setHorizontalSpacing(14)
            form.setVerticalSpacing(8)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for spec in specs:
                widget = self._create(spec)
                self._specs[spec.key] = spec
                self._widgets[spec.key] = widget
                caption = label(spec.label, "FieldLabel")
                if spec.tooltip:
                    caption.setToolTip(spec.tooltip)
                    widget.setToolTip(spec.tooltip)
                if spec.hint:
                    holder = QWidget()
                    v = QVBoxLayout(holder)
                    v.setContentsMargins(0, 0, 0, 0)
                    v.setSpacing(2)
                    v.addWidget(widget)
                    v.addWidget(label(spec.hint, "Hint", wrap=True))
                    form.addRow(caption, holder)
                else:
                    form.addRow(caption, widget)
            card.body.addLayout(form)
            row, col = divmod(index, max(1, columns))
            grid.addWidget(card, row, col, Qt.AlignmentFlag.AlignTop)
        for col in range(max(1, columns)):
            grid.setColumnStretch(col, 1)
        grid.setRowStretch(grid.rowCount(), 1)

    # ------------------------------------------------------------------ widgets
    def _create(self, spec: FieldSpec) -> QWidget:
        kind = spec.kind
        if kind in ("int", "hexint"):
            w = QSpinBox()
            w.setRange(int(spec.minimum), int(min(spec.maximum, 2_147_483_647)))
            w.setSingleStep(int(spec.step) or 1)
            if kind == "hexint":
                w.setDisplayIntegerBase(16)
                w.setPrefix("0x")
            elif spec.suffix:
                w.setSuffix(f" {spec.suffix}")
            w.valueChanged.connect(self._emit)
        elif kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(spec.decimals)
            w.setRange(spec.minimum, spec.maximum)
            w.setSingleStep(spec.step)
            if spec.suffix:
                w.setSuffix(f" {spec.suffix}")
            w.valueChanged.connect(self._emit)
        elif kind == "bool":
            w = QCheckBox()
            w.toggled.connect(self._emit)
        elif kind in ("choice", "combo_edit"):
            w = QComboBox()
            w.setEditable(kind == "combo_edit")
            for value, text in spec.choices:
                w.addItem(text, value)
            w.currentIndexChanged.connect(self._emit)
            if kind == "combo_edit":
                w.editTextChanged.connect(self._emit)
        elif kind == "multiline":
            w = QPlainTextEdit()
            w.setFixedHeight(86)
            w.setPlaceholderText(spec.placeholder)
            w.textChanged.connect(self._emit)
        else:
            w = QLineEdit()
            if kind == "password":
                w.setEchoMode(QLineEdit.EchoMode.Password)
            if spec.placeholder:
                w.setPlaceholderText(spec.placeholder)
            w.textChanged.connect(self._emit)
            if kind == "address":
                w.setMaximumWidth(220)
                w.textChanged.connect(lambda _t, key=spec.key: self._validate_address(key))
        return w

    def _emit(self, *_args) -> None:
        if not self._loading:
            self.changed.emit()

    def _validate_address(self, key: str) -> str:
        widget = self._widgets[key]
        text = widget.text().strip()
        problem = self._address_validator(text) if (text and self._address_validator) else ""
        widget.setProperty("invalid", "true" if problem else "false")
        widget.setToolTip(problem or self._specs[key].tooltip)
        repolish(widget)
        return problem

    # ------------------------------------------------------------------ values
    def widget(self, key: str) -> QWidget:
        return self._widgets[key]

    def set_choices(self, key: str, choices: list[tuple[Any, str]]) -> None:
        widget = self._widgets[key]
        current = self._get(key)
        self._loading = True
        try:
            widget.clear()
            for value, text in choices:
                widget.addItem(text, value)
            self._set(key, current)
        finally:
            self._loading = False

    def _get(self, key: str) -> Any:
        widget = self._widgets[key]
        kind = self._specs[key].kind
        if kind in ("int", "hexint", "float"):
            return widget.value()
        if kind == "bool":
            return widget.isChecked()
        if kind == "choice":
            return widget.currentData()
        if kind == "combo_edit":
            index = widget.findText(widget.currentText())
            data = widget.itemData(index) if index >= 0 else None
            return data if data is not None else widget.currentText().strip()
        if kind == "multiline":
            return widget.toPlainText()
        return widget.text().strip() if kind != "password" else widget.text()

    def _set(self, key: str, value: Any) -> None:
        widget = self._widgets[key]
        kind = self._specs[key].kind
        if kind in ("int", "hexint"):
            widget.setValue(int(value or 0))
        elif kind == "float":
            widget.setValue(float(value or 0.0))
        elif kind == "bool":
            widget.setChecked(bool(value))
        elif kind == "choice":
            index = widget.findData(value)
            if index < 0:
                widget.addItem(f"{value} (unknown)", value)
                index = widget.count() - 1
            widget.setCurrentIndex(index)
        elif kind == "combo_edit":
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
            else:
                widget.setEditText(str(value or ""))
        elif kind == "multiline":
            widget.setPlainText(str(value or ""))
        else:
            widget.setText("" if value is None else str(value))
            if kind == "address":
                self._validate_address(key)

    def load(self, values: dict[str, Any]) -> None:
        self._loading = True
        try:
            for key in self._widgets:
                if key in values:
                    self._set(key, values[key])
        finally:
            self._loading = False
        self._loaded = self.values()

    def load_dataclass(self, obj: Any) -> None:
        self.load({f.name: getattr(obj, f.name) for f in fields(obj)})

    def values(self) -> dict[str, Any]:
        return {key: self._get(key) for key in self._widgets}

    def apply_to(self, obj: Any) -> Any:
        names = {f.name for f in fields(obj)}
        return replace(obj, **{k: v for k, v in self.values().items() if k in names})

    def is_dirty(self) -> bool:
        return self.values() != self._loaded

    def mark_clean(self) -> None:
        self._loaded = self.values()

    def address_errors(self) -> list[str]:
        errors = []
        for key, spec in self._specs.items():
            if spec.kind == "address":
                problem = self._validate_address(key)
                if problem:
                    errors.append(f"{spec.label}: {problem}")
        return errors

    def set_field_enabled(self, key: str, enabled: bool) -> None:
        self._widgets[key].setEnabled(enabled)

    def set_all_enabled(self, enabled: bool) -> None:
        for widget in self._widgets.values():
            widget.setEnabled(enabled)
