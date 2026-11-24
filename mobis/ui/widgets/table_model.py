"""Generic table model for lists of dictionaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from mobis.app.styles import SOFT
from mobis.utils.timeutil import fmt_datetime

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_ROLE = Qt.ItemDataRole.UserRole + 2


@dataclass
class Column:
    key: str
    title: str
    width: Optional[int] = None
    formatter: Optional[Callable[[Any, dict], str]] = None
    align: Optional[Qt.AlignmentFlag] = None
    level: Optional[Callable[[Any, dict], Optional[str]]] = None


def _default_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return fmt_datetime(value)
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return str(value)


def _sort_key(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    return str(value).casefold()


class RecordTableModel(QAbstractTableModel):
    def __init__(self, columns: list[Column], parent=None):
        super().__init__(parent)
        self.columns = columns
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[dict]:
        return list(self._rows)

    def row_at(self, row: int) -> Optional[dict]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section].title
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = self.columns[index.column()]
        value = row.get(column.key)
        if role == Qt.ItemDataRole.DisplayRole:
            return column.formatter(value, row) if column.formatter else _default_text(value)
        if role == Qt.ItemDataRole.ToolTipRole:
            text = column.formatter(value, row) if column.formatter else _default_text(value)
            return text if len(text) > 30 else None
        if role == Qt.ItemDataRole.TextAlignmentRole and column.align is not None:
            return int(column.align | Qt.AlignmentFlag.AlignVCenter)
        if role in (Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole) and column.level is not None:
            level = column.level(value, row)
            if level:
                fg, bg = SOFT.get(level, SOFT["neutral"])
                return QBrush(QColor(bg if role == Qt.ItemDataRole.BackgroundRole else fg))
        if role == SORT_ROLE:
            return _sort_key(value)
        if role == ROW_ROLE:
            return row
        return None


def make_table(model: RecordTableModel, sortable: bool = True) -> tuple[QTableView, QSortFilterProxyModel]:
    proxy = QSortFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.setSortRole(SORT_ROLE)
    view = QTableView()
    view.setModel(proxy)
    # Keep the model order (e.g. newest first) until the user clicks a column header.
    view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
    view.setSortingEnabled(sortable)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(30)
    header = view.horizontalHeader()
    header.setStretchLastSection(True)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setHighlightSections(False)
    for i, column in enumerate(model.columns):
        if column.width:
            view.setColumnWidth(i, column.width)
    return view, proxy


def selected_row(view: QTableView) -> Optional[dict]:
    indexes = view.selectionModel().selectedRows() if view.selectionModel() else []
    if not indexes:
        return None
    return indexes[0].data(ROW_ROLE)
