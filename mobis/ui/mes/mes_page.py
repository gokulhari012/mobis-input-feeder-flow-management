"""MES integration configuration, synchronisation status and queue management."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from mobis.app.styles import STATUS_LEVEL
from mobis.core.config_models import MesConfig
from mobis.core.permissions import Permission
from mobis.integrations.mes_client import join_url, validate_mes_config
from mobis.integrations.mes_payload import preview_body
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
    show_warning,
)
from mobis.ui.widgets.config_form import ConfigForm, FieldSpec
from mobis.ui.widgets.table_model import Column, RecordTableModel, make_table, selected_row
from mobis.utils.timeutil import fmt_datetime

SECTIONS = [
    ("Connection", [
        FieldSpec("enabled", "MES synchronisation enabled", "bool"),
        FieldSpec("base_url", "API base URL", placeholder="https://mes.example.local/api"),
        FieldSpec("endpoint", "Endpoint", placeholder="e.g. material-loadings (placeholder)"),
        FieldSpec("http_method", "HTTP method", "choice", [("POST", "POST"), ("PUT", "PUT"), ("PATCH", "PATCH")]),
        FieldSpec("health_endpoint", "Health check endpoint", placeholder="optional, used by Test Connection"),
        FieldSpec("timeout_s", "Connection timeout", "float", minimum=1, maximum=300, suffix="s"),
        FieldSpec("verify_tls", "Verify TLS certificate", "bool"),
    ]),
    ("Authentication", [
        FieldSpec("auth_type", "Authentication type", "choice", [
            ("NONE", "None"), ("BEARER", "Bearer token"), ("API_KEY", "API key header"),
            ("BASIC", "Basic (username / password)")]),
        FieldSpec("api_token", "API token / key", "password"),
        FieldSpec("api_key_header", "API key header name"),
        FieldSpec("username", "Username"),
        FieldSpec("password", "Password", "password"),
    ]),
    ("Request Format", [
        FieldSpec("headers_json", "Additional headers (JSON)", "multiline", placeholder='{"X-Plant": "P1"}'),
        FieldSpec("payload_mode", "Payload layout", "choice", [("ENVELOPE", "Envelope object with record list"),
                                                                ("ARRAY", "Plain JSON array of records")]),
        FieldSpec("records_key", "Record list key (envelope)"),
        FieldSpec("field_mapping_json", "Field name mapping (JSON)", "multiline",
                  placeholder='{"transaction_id": "txnId", "operator": ""}',
                  hint="Rename internal fields to MES names; an empty name removes the field"),
    ]),
    ("Synchronisation", [
        FieldSpec("sync_interval_s", "Synchronisation interval", "int", minimum=5, maximum=86400, suffix="s"),
        FieldSpec("batch_size", "Batch size (records per request)", "int", minimum=1, maximum=1000),
        FieldSpec("retry_interval_s", "Retry interval", "int", minimum=1, maximum=86400, suffix="s"),
        FieldSpec("max_retry_count", "Maximum retries (HTTP errors)", "int", minimum=1, maximum=1000,
                  hint="An unreachable MES never counts as a retry - records stay pending"),
        FieldSpec("treat_409_as_synced", "HTTP 409 = already received", "bool"),
        FieldSpec("include_rejected", "Also send rejected (NG) transactions", "bool"),
        FieldSpec("include_cancelled", "Also send cancelled transactions", "bool"),
    ]),
]

QUEUE_COLUMNS = [
    Column("id", "Queue #", 80, align=Qt.AlignmentFlag.AlignRight),
    Column("transaction_no", "Transaction", 170),
    Column("status", "Status", 85, level=lambda v, r: STATUS_LEVEL.get(v)),
    Column("attempt_count", "Attempts", 70, align=Qt.AlignmentFlag.AlignRight),
    Column("error_count", "HTTP errors", 85, align=Qt.AlignmentFlag.AlignRight),
    Column("last_attempt_at", "Last attempt", 140),
    Column("next_attempt_at", "Next attempt", 140),
    Column("last_http_status", "HTTP", 55),
    Column("synced_at", "Synced at", 140),
    Column("last_error", "Last error / note"),
]


class TextDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        edit = QPlainTextEdit(text)
        edit.setReadOnly(True)
        edit.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        layout.addWidget(edit)
        close = button("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


class MesPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.mes = ctx.mes
        self.root.addWidget(PageHeader("MES Integration",
                                       "Local-first synchronisation of material loading records with the MES."))
        self.root.addWidget(MessageBox(
            "The company MES API documentation has not been provided. Endpoint, payload layout, field names and "
            "authentication are <b>configurable placeholders</b>. Records are always stored locally first and "
            "remain queued until MES confirms them; <code>transaction_uuid</code> is the idempotency key."))

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.form = ConfigForm(SECTIONS)
        left_layout.addWidget(self.form)
        actions = QHBoxLayout()
        self.btn_preview = button("Preview Payload")
        self.btn_preview.clicked.connect(self._preview)
        self.btn_test = button("Test Connection")
        self.btn_test.clicked.connect(self._test)
        self.btn_revert = button("Revert")
        self.btn_revert.clicked.connect(self._load)
        self.btn_save = button("Save Configuration", "primary")
        self.btn_save.clicked.connect(self._save)
        for btn in (self.btn_preview, self.btn_test):
            actions.addWidget(btn)
        actions.addStretch(1)
        actions.addWidget(self.btn_revert)
        actions.addWidget(self.btn_save)
        left_layout.addLayout(actions)
        body.addWidget(scroll_wrap(left), 2)

        right = QVBoxLayout()
        status = Card("Synchronisation Status")
        self.status = KeyValueGrid(columns=2)
        for key, caption in (("state", "MES status"), ("total", "Total records"), ("pending", "Pending"),
                             ("syncing", "Syncing"), ("synced", "Synced"), ("failed", "Failed"),
                             ("last_success", "Last successful sync"), ("last_attempt", "Last attempt"),
                             ("last_error", "Last error")):
            self.status.add(key, caption)
        status.body.addWidget(self.status)
        self.btn_sync = button("Sync Now", "primary")
        self.btn_sync.clicked.connect(self._sync_now)
        status.body.addWidget(self.btn_sync, 0, Qt.AlignmentFlag.AlignLeft)
        right.addWidget(status)

        queue = Card("Synchronisation Queue")
        bar = QHBoxLayout()
        bar.addWidget(label("Status", "FieldLabel"))
        self.queue_filter = QComboBox()
        for value, text in (("", "All"), ("PENDING", "Pending"), ("SYNCING", "Syncing"), ("SYNCED", "Synced"),
                            ("FAILED", "Failed")):
            self.queue_filter.addItem(text, value)
        self.queue_filter.currentIndexChanged.connect(self._load_queue)
        bar.addWidget(self.queue_filter)
        bar.addStretch(1)
        self.btn_retry_selected = button("Retry Selected")
        self.btn_retry_selected.clicked.connect(self._retry_selected)
        self.btn_retry_all = button("Retry All Failed", "warning")
        self.btn_retry_all.clicked.connect(self._retry_all)
        self.btn_payload = button("View Payload")
        self.btn_payload.clicked.connect(self._view_payload)
        for btn in (self.btn_retry_selected, self.btn_retry_all, self.btn_payload):
            bar.addWidget(btn)
        queue.body.addLayout(bar)
        self.queue_model = RecordTableModel(QUEUE_COLUMNS)
        self.queue_table, _p = make_table(self.queue_model)
        queue.body.addWidget(self.queue_table, 1)
        right.addWidget(queue, 1)
        body.addLayout(right, 3)
        self.root.addLayout(body, 1)

        self.mes.status_changed.connect(self._render_status)
        ctx.hub.mes_queue_changed.connect(self._load_queue)

    # ------------------------------------------------------------------ page
    def on_show(self) -> None:
        self._load()
        self.mes.refresh_counts()
        self._load_queue()
        self.on_user_changed()

    def on_user_changed(self) -> None:
        can_configure = self.ctx.auth.has_permission(Permission.CONFIGURE_MES)
        self.form.set_all_enabled(can_configure)
        for btn in (self.btn_save, self.btn_revert, self.btn_test):
            btn.setEnabled(can_configure)

    def can_leave(self) -> bool:
        if self.form.is_dirty():
            return confirm(self, "Unsaved Changes", "Discard the unsaved MES configuration changes?",
                           yes_text="Discard")
        return True

    def _load(self) -> None:
        self.form.load_dataclass(self.ctx.config.mes())

    def _collect(self) -> MesConfig:
        return self.form.apply_to(self.ctx.config.mes())

    def _save(self) -> None:
        cfg = self._collect()
        errors = validate_mes_config(cfg)
        if errors:
            show_error(self, "MES Configuration", "\n".join(errors))
            return
        changed = self.ctx.config.save("mes", cfg, self.username)
        self.form.mark_clean()
        show_info(self, "MES Configuration", "Configuration saved." if changed else "No changes.")

    def _preview(self) -> None:
        cfg = self._collect()
        try:
            body = preview_body(cfg, self.ctx.config.app().station_code)
        except ValueError as exc:
            show_error(self, "Preview Payload", str(exc))
            return
        url = join_url(cfg.base_url, cfg.endpoint) or "(base URL not configured)"
        TextDialog("Payload Preview", f"{cfg.http_method} {url}\n\n{body}", self).exec()

    def _test(self) -> None:
        cfg = self._collect()
        errors = [e for e in validate_mes_config(cfg) if "required when" not in e]
        if not cfg.base_url.strip():
            show_warning(self, "Test Connection", "Enter the MES base URL first.")
            return
        if errors:
            show_error(self, "Test Connection", "\n".join(errors))
            return
        self.btn_test.setEnabled(False)
        self.btn_test.setText("Testing…")

        def done(result):
            self.btn_test.setEnabled(True)
            self.btn_test.setText("Test Connection")
            if result.reachable and result.ok:
                show_info(self, "Test Connection",
                          f"MES reachable (HTTP {result.status_code}, {result.elapsed_ms:.0f} ms).")
            elif result.reachable:
                show_warning(self, "Test Connection", f"MES answered with an error: {result.error}")
            else:
                show_error(self, "Test Connection", result.error)

        self.mes.test_connection(cfg, done)

    # ------------------------------------------------------------------ status / queue
    def _render_status(self) -> None:
        level, text = self.mes.status()
        counts = self.mes.counts or {}
        s = self.status
        s.set("state", text, level)
        s.set("total", str(counts.get("TOTAL", 0)))
        s.set("pending", str(counts.get("PENDING", 0)), "warn" if counts.get("PENDING") else None)
        s.set("syncing", str(counts.get("SYNCING", 0)))
        s.set("synced", str(counts.get("SYNCED", 0)), "ok" if counts.get("SYNCED") else None)
        s.set("failed", str(counts.get("FAILED", 0)), "ng" if counts.get("FAILED") else None)
        s.set("last_success", fmt_datetime(self.mes.last_success_at))
        s.set("last_attempt", fmt_datetime(self.mes.last_attempt_at))
        report = self.mes.last_report
        s.set("last_error", (report.skipped_reason if report and report.skipped_reason else self.mes.last_error))
        can_operate = self.ctx.auth.has_permission(Permission.MES_OPERATIONS)
        self.btn_sync.setEnabled(can_operate)
        self.btn_retry_all.setEnabled(can_operate and counts.get("FAILED", 0) > 0)
        self.btn_retry_selected.setEnabled(can_operate)

    def _load_queue(self) -> None:
        if not self.isVisible():
            return
        with self.ctx.db.read_session() as s:
            rows = self.ctx.repos.mes_queue.list(s, self.queue_filter.currentData() or "", limit=2000)
        self.queue_model.set_rows(rows)
        self._render_status()

    def _sync_now(self) -> None:
        self.mes.sync_now()
        show_info(self, "Sync Now", "Synchronisation started in the background.")

    def _retry_selected(self) -> None:
        row = selected_row(self.queue_table)
        if row is None or row["status"] != "FAILED":
            show_info(self, "Retry", "Select a FAILED record.")
            return
        self.mes.retry_failed([row["id"]])
        self._load_queue()

    def _retry_all(self) -> None:
        if confirm(self, "Retry All Failed", "Queue all FAILED records for synchronisation again?"):
            count = self.mes.retry_failed(None)
            self._load_queue()
            show_info(self, "Retry", f"{count} record(s) queued again.")

    def _view_payload(self) -> None:
        row = selected_row(self.queue_table)
        if row is None:
            return
        try:
            text = json.dumps(json.loads(row["payload"]), indent=2, ensure_ascii=False)
        except ValueError:
            text = row["payload"]
        TextDialog(f"Stored record – {row['transaction_no']}", text, self).exec()
