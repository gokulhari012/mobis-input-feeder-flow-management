"""Small reusable widgets and helpers shared by all pages."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mobis.app.styles import DOT, FONT_FAMILY, LEVELS, RESULT_LEVEL, SOFT


def repolish(widget: QWidget) -> None:
    """Re-apply the style sheet after a dynamic property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def font(point_size: float, bold: bool = False) -> QFont:
    f = QFont(FONT_FAMILY)
    f.setPointSizeF(point_size)
    f.setBold(bold)
    return f


def button(text: str, kind: str = "default", large: bool = False, tooltip: str = "",
           focusable: bool = True) -> QPushButton:
    btn = QPushButton(text)
    if kind != "default":
        btn.setProperty("kind", kind)
    if large:
        btn.setProperty("size", "large")
    if tooltip:
        btn.setToolTip(tooltip)
    if not focusable:
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def label(text: str = "", object_name: str = "", wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if object_name:
        lbl.setObjectName(object_name)
    if wrap:
        lbl.setWordWrap(True)
    return lbl


class ElidedLabel(QLabel):
    """Single-line label that shrinks with "…" instead of forcing its parent wider."""

    def __init__(self, text: str = "", object_name: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        if object_name:
            self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(80)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        self.style().drawItemText(painter, self.rect(), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                                  self.palette(), self.isEnabled(), text, self.foregroundRole())


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: #E3E8EF; background: #E3E8EF; max-height: 1px;")
    return line


def scroll_wrap(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    widget.setObjectName(widget.objectName() or "ScrollContent")
    area.setWidget(widget)
    return area


# --------------------------------------------------------------------------- #
class Card(QFrame):
    """White panel with an optional title and header actions."""

    def __init__(self, title: str = "", parent: Optional[QWidget] = None, spacing: int = 10):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 14)
        outer.setSpacing(spacing)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.header.addWidget(self.title_label)
        self.header.addStretch(1)
        outer.addLayout(self.header)
        self.title_label.setVisible(bool(title))
        self.body = QVBoxLayout()
        self.body.setSpacing(spacing)
        outer.addLayout(self.body, 1)

    def add_header_widget(self, widget: QWidget) -> None:
        self.header.addWidget(widget)

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))


class MessageBox(QFrame):
    """Inline information / warning box."""

    def __init__(self, text: str, kind: str = "info", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("InfoBox" if kind == "info" else "WarnBox")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.text)

    def set_text(self, text: str) -> None:
        self.text.setText(text)


class StatusPill(QFrame):
    """Coloured dot + caption + value (used in the header and status cards)."""

    def __init__(self, caption: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("StatusPill")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 12, 4)
        layout.setSpacing(6)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        self.caption = QLabel(caption)
        self.caption.setStyleSheet("color: #5B6776;")
        self.value = QLabel("—")
        self.value.setFont(font(9.5, True))
        layout.addWidget(self.dot)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        self.set_state("neutral", "—")

    def set_state(self, level: str, text: str, tooltip: str = "") -> None:
        fg, bg = SOFT.get(level, SOFT["neutral"])
        self.setStyleSheet(
            f"QFrame#StatusPill {{ background: {bg}; border: 1px solid {bg}; border-radius: 12px; }}"
        )
        self.dot.setStyleSheet(f"background: {DOT.get(level, DOT['neutral'])}; border-radius: 5px;")
        self.value.setStyleSheet(f"color: {fg};")
        self.value.setText(text)
        self.setToolTip(tooltip)


class ResultBanner(QFrame):
    """Large status banner of the operation screen (OK / NG / instructions)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ResultBanner")
        self.setMinimumHeight(104)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 12)
        layout.setSpacing(4)
        self.title = QLabel()
        self.title.setFont(font(28, True))
        self.message = QLabel()
        self.message.setFont(font(13))
        self.message.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.message, 1)
        self.set_state("neutral", "")

    def set_state(self, level: str, title: str, message: str = "") -> None:
        fg, bg = LEVELS.get(level, LEVELS["neutral"])
        self.setStyleSheet(
            f"QFrame#ResultBanner {{ background: {bg}; border-radius: 8px; }}"
            f"QFrame#ResultBanner QLabel {{ color: {fg}; background: transparent; }}"
        )
        self.title.setText(title)
        self.message.setText(message)
        self.message.setVisible(bool(message))


class ResultChip(QLabel):
    def __init__(self, text: str = "", level: str = "neutral", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFont(font(10, True))
        self.set_level(level, text)

    def set_level(self, level: str, text: str) -> None:
        fg, bg = SOFT.get(level, SOFT["neutral"])
        self.setStyleSheet(f"background: {bg}; color: {fg}; border-radius: 4px; padding: 3px 10px;")
        self.setText(text)

    def set_result(self, result: str) -> None:
        text = {"NA": "N/A"}.get(result, result)
        self.set_level(RESULT_LEVEL.get(result, "neutral"), text)


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = label(title, "PageTitle")
        texts.addWidget(self.title)
        self.subtitle = label(subtitle, "PageSubtitle", wrap=True)
        self.subtitle.setVisible(bool(subtitle))
        texts.addWidget(self.subtitle)
        layout.addLayout(texts, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)


class KeyValueGrid(QWidget):
    """Label / value pairs laid out in columns."""

    def __init__(self, columns: int = 1, value_font: Optional[QFont] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(6)
        self._columns = max(1, columns)
        self._count = 0
        self._values: dict[str, QLabel] = {}
        self._value_font = value_font

    def add(self, key: str, caption: str, value: str = "—") -> QLabel:
        row, col = divmod(self._count, self._columns)
        cap = label(caption, "FieldLabel")
        val = label(value, "ValueLabel")
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        val.setWordWrap(True)
        if self._value_font is not None:
            val.setFont(self._value_font)
        self._grid.addWidget(cap, row, col * 2, Qt.AlignmentFlag.AlignTop)
        self._grid.addWidget(val, row, col * 2 + 1)
        self._grid.setColumnStretch(col * 2 + 1, 1)
        self._values[key] = val
        self._count += 1
        return val

    def set(self, key: str, text: str, level: Optional[str] = None) -> None:
        val = self._values.get(key)
        if val is None:
            return
        val.setText(text if text not in (None, "") else "—")
        if level:
            val.setStyleSheet(f"color: {SOFT.get(level, SOFT['neutral'])[0]};")
        else:
            val.setStyleSheet("")


class LevelBar(QProgressBar):
    """Tank fill level."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setTextVisible(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_level(self, quantity: Optional[float], capacity: Optional[float]) -> None:
        if quantity is None:
            self.setValue(0)
            self.setFormat("—")
            self.setProperty("level", "")
        elif not capacity:
            self.setValue(0)
            self.setFormat(f"{quantity:,.1f} kg (capacity not set)")
            self.setProperty("level", "")
        else:
            ratio = quantity / capacity
            self.setValue(int(max(0.0, min(1.0, ratio)) * 1000))
            self.setFormat(f"{ratio * 100:.1f} %  ({quantity:,.0f} / {capacity:,.0f} kg)")
            self.setProperty("level", "ng" if ratio > 1.0 else "warn" if ratio >= 0.9 else "")
        repolish(self)


# --------------------------------------------------------------------------- #
# Message helpers
# --------------------------------------------------------------------------- #
def confirm(parent: QWidget, title: str, text: str, danger: bool = False, yes_text: str = "Yes") -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    yes = box.addButton(yes_text, QMessageBox.ButtonRole.AcceptRole)
    if danger:
        yes.setProperty("kind", "danger")
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(QMessageBox.StandardButton.NoButton)
    box.exec()
    return box.clickedButton() is yes


def show_error(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def show_warning(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


def show_info(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def fmt_kg(value: Optional[float], decimals: int = 1) -> str:
    return "—" if value is None else f"{value:,.{decimals}f} kg"
