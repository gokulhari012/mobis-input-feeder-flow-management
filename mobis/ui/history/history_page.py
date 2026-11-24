"""Material history / traceability, scan log, system events and audit log."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mobis.app.styles import STATUS_LEVEL
from mobis.core.models import EquipmentCategory
from mobis.core.permissions import Permission
from mobis.database.repositories import TransactionFilter
from mobis.services.export_service import export_rows
from mobis.ui.base_page import BasePage
from mobis.ui.widgets.common import Card, KeyValueGrid, PageHeader, button, label, show_error, show_info
from mobis.ui.widgets.table_model import Column, RecordTableModel, make_table, selected_row
from mobis.utils.timeutil import fmt_datetime, local_day_end_utc, local_day_start_utc, local_stamp

RIGHT = Qt.AlignmentFlag.AlignRight


def _num(value, _row=None) -> str:
    return "" if value is None else f"{value:,.1f}"


def _status_level(value, _row) -> Optional[str]:
    return STATUS_LEVEL.get(value)


def _validation_level(value, _row) -> Optional[str]:
    return {"OK": "ok", "NG": "ng"}.get(value)


TX_COLUMNS = [
    Column("transaction_no", "Transaction ID", 170),
    Column("started_at", "Date / Time", 145),
    Column("operator_username", "Operator", 85),
    Column("bag_type_code", "Bag Type", 95),
    Column("colour", "Colour", 75),
    Column("bag_barcode", "Bag Barcode", 160),
    Column("hopper_code", "Hopper", 75),
    Column("silo_code", "Silo", 75),
    Column("distribution_tank_code", "Dist. Tank", 80),
    Column("mold_code", "Mold", 70),
    Column("bag_weight_kg", "Weight (kg)", 85, _num, RIGHT),
    Column("quantity_before_kg", "Existing (kg)", 95, _num, RIGHT),
    Column("quantity_after_kg", "Updated (kg)", 95, _num, RIGHT),
    Column("validation_status", "Validation", 80, level=_validation_level),
    Column("status", "Status", 100, level=_status_level),
    Column("mes_status", "MES", 90, level=_status_level),
]

TX_EXPORT = [
    ("transaction_no", "Transaction ID", None), ("transaction_uuid", "Transaction UUID", None),
    ("station_code", "Station", None), ("started_at", "Started", None), ("completed_at", "Completed", None),
    ("operator_username", "Operator", None), ("bag_type_code", "Bag Type", None),
    ("bag_type_name", "Bag Type Name", None), ("material_type", "Material", None), ("colour", "Colour", None),
    ("bag_barcode", "Bag Barcode", None), ("bag_lot", "Lot", None), ("bag_serial", "Serial", None),
    ("bag_weight_kg", "Bag Weight (kg)", None), ("hopper_code", "Hopper", None), ("silo_code", "Silo", None),
    ("distribution_tank_code", "Distribution Tank", None), ("mold_code", "Mold / Machine", None),
    ("destination_code", "Destination", None), ("quantity_before_kg", "Existing Qty (kg)", None),
    ("quantity_after_kg", "Updated Qty (kg)", None), ("validation_status", "Validation", None),
    ("status", "Status", None), ("ng_count", "NG Count", None), ("confirmation_source", "Confirmation", None),
    ("mes_status", "MES Status", None), ("notes", "Notes", None), ("last_error", "Last Error", None),
]

SCAN_COLUMNS = [
    Column("scanned_at", "Date / Time", 145),
    Column("transaction_no", "Transaction", 170),
    Column("stage", "Stage", 120),
    Column("barcode", "Barcode", 170),
    Column("result", "Result", 80, level=lambda v, r: {"OK": "ok", "NG": "ng", "IGNORED": "warn"}.get(v)),
    Column("input_source", "Source", 120),
    Column("operator_username", "Operator", 85),
    Column("message", "Message"),
]

EVENT_COLUMNS = [
    Column("occurred_at", "Date / Time", 145),
    Column("severity", "Severity", 85, level=_status_level),
    Column("source", "Source", 110),
    Column("event_type", "Event", 200),
    Column("message", "Message"),
]

AUDIT_COLUMNS = [
    Column("occurred_at", "Date / Time", 145),
    Column("username", "User", 90),
    Column("category", "Category", 100),
    Column("action", "Action", 120),
    Column("entity_id", "Entity ID", 80),
    Column("details", "Details"),
]


def _combo(items: Iterable[tuple[str, str]], all_text: str = "All") -> QComboBox:
    combo = QComboBox()
    combo.addItem(all_text, "")
    for value, text in items:
        combo.addItem(text, value)
    combo.setMinimumWidth(110)
    return combo


def _date_edit(day: date) -> QDateEdit:
    edit = QDateEdit(QDate(day.year, day.month, day.day))
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    return edit


def _qdate(edit: QDateEdit) -> date:
    return edit.date().toPython()


def export_with_dialog(parent: QWidget, default_name: str, columns, rows_fn: Callable[[], Iterable[dict]],
                       exports_dir: Path, audit=None, what: str = "") -> None:
    path, _selected = QFileDialog.getSaveFileName(
        parent, "Export", str(exports_dir / default_name), "Excel workbook (*.xlsx);;CSV file (*.csv)"
    )
    if not path:
        return
    try:
        count = export_rows(Path(path), columns, rows_fn())
    except Exception as exc:  # noqa: BLE001
        show_error(parent, "Export failed", str(exc))
        return
    if audit is not None:
        audit.log("DATA", "EXPORT", f"{count} {what} rows exported to {path}")
    show_info(parent, "Export complete", f"{count} rows exported to:\n{path}")


class LogTab(QWidget):
    """Date range + filters + text search + table (+ optional detail text)."""

    def __init__(self, columns: list[Column], query: Callable[..., list[dict]], filters: list[tuple[str, str, list]],
                 detail: Optional[Callable[[dict], str]] = None, export_name: str = "", ctx=None):
        super().__init__()
        self._query = query
        self._detail = detail
        self._export_name = export_name
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        bar = QHBoxLayout()
        today = date.today()
        self.date_from = _date_edit(today - timedelta(days=7))
        self.date_to = _date_edit(today)
        bar.addWidget(label("From", "FieldLabel"))
        bar.addWidget(self.date_from)
        bar.addWidget(label("To", "FieldLabel"))
        bar.addWidget(self.date_to)
        self.filters: dict[str, QComboBox] = {}
        for key, caption, choices in filters:
            bar.addWidget(label(caption, "FieldLabel"))
            combo = _combo([(c, c) for c in choices])
            self.filters[key] = combo
            bar.addWidget(combo)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Search text…")
        self.text.returnPressed.connect(self.search)
        bar.addWidget(self.text, 1)
        search = button("Search", "primary")
        search.clicked.connect(self.search)
        bar.addWidget(search)
        if export_name:
            self.export_btn = button("Export…")
            self.export_btn.clicked.connect(self._export)
            bar.addWidget(self.export_btn)
        layout.addLayout(bar)
        self.model = RecordTableModel(columns)
        self.table, _proxy = make_table(self.model)
        self.count = label("", "Muted")
        if detail:
            splitter = QSplitter(Qt.Orientation.Vertical)
            splitter.addWidget(self.table)
            self.detail_text = QPlainTextEdit()
            self.detail_text.setReadOnly(True)
            splitter.addWidget(self.detail_text)
            splitter.setSizes([400, 160])
            layout.addWidget(splitter, 1)
            self.table.selectionModel().selectionChanged.connect(self._show_detail)
        else:
            layout.addWidget(self.table, 1)
        layout.addWidget(self.count)

    def _params(self) -> dict[str, Any]:
        return {
            "date_from": local_day_start_utc(_qdate(self.date_from)),
            "date_to": local_day_end_utc(_qdate(self.date_to)),
            "text": self.text.text(),
            **{key: combo.currentData() or "" for key, combo in self.filters.items()},
        }

    def search(self) -> None:
        rows = self._query(**self._params())
        self.model.set_rows(rows)
        self.count.setText(f"{len(rows)} record(s)")

    def _show_detail(self) -> None:
        row = selected_row(self.table)
        self.detail_text.setPlainText(self._detail(row) if row else "")

    def _export(self) -> None:
        columns = [(c.key, c.title, None) for c in self.model.columns]
        export_with_dialog(self, f"{self._export_name}_{local_stamp()}.xlsx", columns,
                           lambda: self._query(limit=1_000_000, **self._params()),
                           self.ctx.paths.exports_dir, self.ctx.audit, self._export_name)


class TransactionsTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        today = date.today()
        self.date_from = _date_edit(today - timedelta(days=7))
        self.date_to = _date_edit(today)
        self.bag_type = _combo([])
        self.colour = _combo([])
        self.hopper = _combo([])
        self.silo = _combo([])
        self.tank = _combo([])
        self.status = _combo([(s, s.title().replace("_", " ")) for s in
                              ("COMPLETED", "REJECTED", "CANCELLED", "INTERRUPTED", "IN_PROGRESS")])
        self.validation = _combo([("OK", "OK"), ("NG", "NG"), ("PENDING", "Pending")])
        self.mes = _combo([(s, s.title().replace("_", " ")) for s in
                           ("PENDING", "SYNCING", "SYNCED", "FAILED", "NOT_QUEUED")])
        self.text = QLineEdit()
        self.text.setPlaceholderText("Barcode, transaction ID, lot or serial…")
        self.text.returnPressed.connect(self.search)
        cells = [
            ("From", self.date_from), ("To", self.date_to), ("Bag type", self.bag_type), ("Colour", self.colour),
            ("Status", self.status), ("Validation", self.validation),
            ("Hopper", self.hopper), ("Silo", self.silo), ("Dist. tank", self.tank), ("MES", self.mes),
        ]
        for i, (caption, widget) in enumerate(cells):
            row, col = divmod(i, 6)
            grid.addWidget(label(caption, "FieldLabel"), row, col * 2)
            grid.addWidget(widget, row, col * 2 + 1)
        grid.addWidget(label("Search", "FieldLabel"), 1, 8)
        grid.addWidget(self.text, 1, 9, 1, 3)
        layout.addLayout(grid)

        actions = QHBoxLayout()
        search = button("Search", "primary")
        search.clicked.connect(self.search)
        reset = button("Reset Filters")
        reset.clicked.connect(self.reset_filters)
        actions.addWidget(search)
        actions.addWidget(reset)
        self.count = label("", "Muted")
        actions.addWidget(self.count)
        actions.addStretch(1)
        self.export_btn = button("Export CSV / Excel…")
        self.export_btn.clicked.connect(self._export)
        actions.addWidget(self.export_btn)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.model = RecordTableModel(TX_COLUMNS)
        self.table, _proxy = make_table(self.model)
        self.table.selectionModel().selectionChanged.connect(self._show_detail)
        splitter.addWidget(self.table)

        detail = Card("Transaction Details")
        detail_row = QHBoxLayout()
        self.detail = KeyValueGrid(columns=3)
        for key, caption in (
            ("transaction_no", "Transaction ID"), ("status", "Status"), ("validation_status", "Validation"),
            ("started_at", "Started"), ("completed_at", "Completed"), ("operator_username", "Operator"),
            ("bag_type", "Bag type"), ("colour", "Colour"), ("bag_barcode", "Bag barcode"),
            ("lot", "Lot / serial"), ("bag_weight_kg", "Bag weight"), ("route", "Hopper → destination"),
            ("quantities", "Quantity (kg)"), ("results", "Bag / colour / hopper / silo / tank / mold"),
            ("ng_count", "NG scans"), ("confirmation_source", "Confirmation"), ("mes_status", "MES"),
            ("transaction_uuid", "UUID"), ("notes", "Notes"),
        ):
            self.detail.add(key, caption)
        detail_row.addWidget(self.detail, 3)
        self.scan_model = RecordTableModel([
            Column("scanned_at", "Time", 140), Column("stage", "Stage", 110), Column("barcode", "Barcode", 150),
            Column("result", "Result", 70, level=lambda v, r: {"OK": "ok", "NG": "ng", "IGNORED": "warn"}.get(v)),
            Column("message", "Message"),
        ])
        self.scan_table, _p = make_table(self.scan_model, sortable=False)
        detail_row.addWidget(self.scan_table, 2)
        detail.body.addLayout(detail_row)
        splitter.addWidget(detail)
        splitter.setSizes([420, 260])
        layout.addWidget(splitter, 1)

    def reload_filter_choices(self) -> None:
        def fill(combo: QComboBox, items: list[tuple[str, str]]):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All", "")
            for value, text in items:
                combo.addItem(text, value)
            index = combo.findData(current)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)

        mapping = self.ctx.mapping
        fill(self.bag_type, [(b.code, f"{b.code} – {b.name}") for b in mapping.list_bag_types()])
        with self.ctx.db.read_session() as s:
            colours = set(self.ctx.repos.transactions.distinct_values(s, "colour"))
        colours |= {b.colour for b in mapping.list_bag_types()}
        fill(self.colour, [(c, c) for c in sorted(colours, key=str.casefold)])
        equipment = mapping.list_equipment()
        for combo, category in ((self.hopper, EquipmentCategory.HOPPER), (self.silo, EquipmentCategory.SILO),
                                (self.tank, EquipmentCategory.DISTRIBUTION_TANK)):
            fill(combo, [(e.code, e.name) for e in equipment if e.category == category])

    def reset_filters(self) -> None:
        today = date.today()
        self.date_from.setDate(QDate(*(today - timedelta(days=7)).timetuple()[:3]))
        self.date_to.setDate(QDate(today.year, today.month, today.day))
        for combo in (self.bag_type, self.colour, self.hopper, self.silo, self.tank, self.status, self.validation,
                      self.mes):
            combo.setCurrentIndex(0)
        self.text.clear()
        self.search()

    def _filter(self) -> TransactionFilter:
        return TransactionFilter(
            date_from=local_day_start_utc(_qdate(self.date_from)),
            date_to=local_day_end_utc(_qdate(self.date_to)),
            bag_type_code=self.bag_type.currentData() or "",
            colour=self.colour.currentData() or "",
            hopper_code=self.hopper.currentData() or "",
            silo_code=self.silo.currentData() or "",
            distribution_tank_code=self.tank.currentData() or "",
            status=self.status.currentData() or "",
            validation_status=self.validation.currentData() or "",
            mes_status=self.mes.currentData() or "",
            text=self.text.text(),
        )

    def search(self) -> None:
        limit = self.ctx.config.app().history_max_rows
        with self.ctx.db.read_session() as s:
            rows = self.ctx.repos.transactions.search(s, self._filter(), limit=limit)
        self.model.set_rows(rows)
        suffix = f" (showing the latest {limit}; refine the filter or export for all)" if len(rows) >= limit else ""
        self.count.setText(f"{len(rows)} transaction(s){suffix}")
        self._show_detail()

    def _show_detail(self) -> None:
        row = selected_row(self.table)
        d = self.detail
        if row is None:
            for key in ("transaction_no", "status", "validation_status", "started_at", "completed_at",
                        "operator_username", "bag_type", "colour", "bag_barcode", "lot", "bag_weight_kg", "route",
                        "quantities", "results", "ng_count", "confirmation_source", "mes_status",
                        "transaction_uuid", "notes"):
                d.set(key, "—")
            self.scan_model.set_rows([])
            return
        d.set("transaction_no", row["transaction_no"])
        d.set("status", row["status"], STATUS_LEVEL.get(row["status"]))
        d.set("validation_status", row["validation_status"], _validation_level(row["validation_status"], row))
        d.set("started_at", fmt_datetime(row["started_at"]))
        d.set("completed_at", fmt_datetime(row["completed_at"]) or fmt_datetime(row["ended_at"]))
        d.set("operator_username", row["operator_username"] or "Station")
        d.set("bag_type", f"{row['bag_type_code']} – {row['bag_type_name']} ({row['material_type']})")
        d.set("colour", row["colour"])
        d.set("bag_barcode", row["bag_barcode"])
        d.set("lot", " / ".join(x for x in (row["bag_lot"], row["bag_serial"]) if x) or "—")
        d.set("bag_weight_kg", f"{row['bag_weight_kg']:g} kg")
        route = " → ".join(x for x in (row["hopper_code"], row["silo_code"], row["distribution_tank_code"],
                                       row["mold_code"]) if x)
        d.set("route", route)
        d.set("quantities", f"{_num(row['quantity_before_kg']) or '—'} → {_num(row['quantity_after_kg']) or '—'}"
              + (f"  ({row['destination_code']})" if row["destination_code"] else ""))
        d.set("results", " / ".join(row[k] for k in ("bag_result", "colour_result", "hopper_result", "silo_result",
                                                      "distribution_result", "mold_result")))
        d.set("ng_count", str(row["ng_count"]), "ng" if row["ng_count"] else None)
        d.set("confirmation_source", row["confirmation_source"])
        d.set("mes_status", row["mes_status"], STATUS_LEVEL.get(row["mes_status"]))
        d.set("transaction_uuid", row["transaction_uuid"])
        d.set("notes", "\n".join(x for x in (row["notes"], row["last_error"]) if x))
        with self.ctx.db.read_session() as s:
            self.scan_model.set_rows(self.ctx.repos.transactions.scans_for(s, row["id"]))

    def _export(self) -> None:
        f = self._filter()

        def rows():
            with self.ctx.db.read_session() as s:
                yield from self.ctx.repos.transactions.iter_search(s, f)

        export_with_dialog(self, f"material_transactions_{local_stamp()}.xlsx", TX_EXPORT, rows,
                           self.ctx.paths.exports_dir, self.ctx.audit, "transaction")


class HistoryPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.root.addWidget(PageHeader("Material History & Traceability",
                                       "All material loading transactions, scans, system events and audit trail."))
        self.tabs = QTabWidget()
        self.transactions = TransactionsTab(ctx)
        self.tabs.addTab(self.transactions, "Material Transactions")

        repos, db = ctx.repos, ctx.db

        def scans(date_from, date_to, text, result="", limit=None):
            with db.read_session() as s:
                return repos.transactions.search_scans(s, date_from, date_to, result, text,
                                                       limit or ctx.config.app().history_max_rows)

        def events(date_from, date_to, text, severity="", source="", limit=None):
            with db.read_session() as s:
                return repos.audit.list_events(s, date_from, date_to, severity, source, text,
                                               limit or ctx.config.app().history_max_rows)

        def audit(date_from, date_to, text, category="", limit=None):
            with db.read_session() as s:
                return repos.audit.list_audit(s, date_from, date_to, category, text,
                                              limit or ctx.config.app().history_max_rows)

        def audit_detail(row: dict) -> str:
            def pretty(value: str) -> str:
                try:
                    return json.dumps(json.loads(value), indent=2, ensure_ascii=False) if value else "—"
                except ValueError:
                    return value

            return (f"{row['details']}\n\nOld value:\n{pretty(row['old_value'])}\n\n"
                    f"New value:\n{pretty(row['new_value'])}")

        self.scans_tab = LogTab(SCAN_COLUMNS, scans, [("result", "Result", ["OK", "NG", "IGNORED"])],
                                export_name="scan_log", ctx=ctx)
        self.events_tab = LogTab(
            EVENT_COLUMNS, events,
            [("severity", "Severity", ["INFO", "WARNING", "ERROR", "CRITICAL"]),
             ("source", "Source", ["PLC", "SCANNER", "MES", "TRANSACTION", "VALIDATION", "ALARM", "DATABASE",
                                   "SYSTEM"])],
            detail=lambda r: f"{r['message']}\n\n{r['details']}", export_name="system_events", ctx=ctx,
        )
        self.audit_tab = LogTab(
            AUDIT_COLUMNS, audit,
            [("category", "Category", ["CONFIG", "BAG_TYPE", "EQUIPMENT", "ROUTING", "INVENTORY", "USER",
                                       "SECURITY", "TRANSACTION", "DATABASE", "DATA"])],
            detail=audit_detail, export_name="audit_log", ctx=ctx,
        )
        self.tabs.addTab(self.scans_tab, "Scan Log")
        self.tabs.addTab(self.events_tab, "System Events")
        self.tabs.addTab(self.audit_tab, "Audit Log")
        self.tabs.currentChanged.connect(self._on_tab)
        self.root.addWidget(self.tabs, 1)
        ctx.hub.transactions_changed.connect(self._on_data_changed)

    def on_show(self) -> None:
        self.on_user_changed()
        self.transactions.reload_filter_choices()
        self._on_tab(self.tabs.currentIndex())

    def on_user_changed(self) -> None:
        can_view_logs = self.ctx.auth.has_permission(Permission.VIEW_EVENTS)
        can_export = self.ctx.auth.has_permission(Permission.EXPORT_HISTORY)
        for index in (2, 3):
            self.tabs.setTabVisible(index, can_view_logs)
        if not can_view_logs and self.tabs.currentIndex() in (2, 3):
            self.tabs.setCurrentIndex(0)
        self.transactions.export_btn.setVisible(can_export)
        for tab in (self.scans_tab, self.events_tab, self.audit_tab):
            tab.export_btn.setVisible(can_export)

    def _on_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is not None:
            widget.search()

    def _on_data_changed(self) -> None:
        if self.isVisible() and self.tabs.currentIndex() == 0:
            self.transactions.search()
