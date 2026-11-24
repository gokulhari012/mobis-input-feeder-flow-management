"""Administration: users, application settings, backup / restore, logs, diagnostics."""

from __future__ import annotations

import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QProcess, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mobis import __version__
from mobis.core.permissions import Permission, Role
from mobis.services import logging_service
from mobis.services.backup_service import validate_backup_file
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
)
from mobis.ui.widgets.config_form import ConfigForm, FieldSpec
from mobis.ui.widgets.table_model import Column, RecordTableModel, make_table, selected_row
from mobis.utils.timeutil import utc_now

APP_SECTIONS = [
    ("Station", [
        FieldSpec("station_code", "Station code", hint="Prefix of transaction IDs and MES station identifier"),
        FieldSpec("station_name", "Station name"),
        FieldSpec("plant_name", "Plant"),
    ]),
    ("Operation", [
        FieldSpec("require_operator_login", "Operators must log in", "bool",
                  hint="When off, the station runs as a shared operator session"),
        FieldSpec("admin_session_timeout_min", "Supervisor / admin auto logout", "int", minimum=0, maximum=480,
                  suffix="min"),
        FieldSpec("mold_verification_enabled", "Enable mold / machine verification", "bool"),
        FieldSpec("auto_new_transaction", "Start next transaction by scanning the next bag", "bool"),
        FieldSpec("confirm_cancel", "Confirm before cancelling a transaction", "bool"),
        FieldSpec("history_max_rows", "History rows shown", "int", minimum=100, maximum=100000),
        FieldSpec("kiosk_fullscreen", "Start in full screen (kiosk)", "bool"),
    ]),
    ("Date & Time Display", [
        FieldSpec("date_format", "Date format", "choice", [("%Y-%m-%d", "2026-11-24"), ("%d.%m.%Y", "24.11.2026"),
                                                           ("%d/%m/%Y", "24/11/2026"), ("%m/%d/%Y", "11/24/2026")]),
        FieldSpec("time_format", "Time format", "choice", [("%H:%M:%S", "14:05:30 (24 h)"),
                                                           ("%I:%M:%S %p", "02:05:30 PM (12 h)")]),
    ]),
]

BACKUP_SECTIONS = [
    ("Automatic Backup", [
        FieldSpec("auto_backup_enabled", "Automatic backup", "bool"),
        FieldSpec("auto_backup_interval_h", "Interval", "int", minimum=1, maximum=720, suffix="h"),
        FieldSpec("backup_retention_count", "Keep automatic backups", "int", minimum=1, maximum=1000),
    ]),
]

USER_COLUMNS = [
    Column("username", "Username", 130),
    Column("full_name", "Full Name", 180),
    Column("role", "Role", 100),
    Column("is_active", "Active", 60, lambda v, r: "Yes" if v else "No", level=lambda v, r: None if v else "neutral"),
    Column("must_change_password", "Must Change Password", 150, lambda v, r: "Yes" if v else ""),
    Column("last_login_at", "Last Login", 150),
    Column("locked_until", "Locked Until"),
]

BACKUP_COLUMNS = [
    Column("name", "Backup File", 360),
    Column("modified", "Created", 160),
    Column("size", "Size", 100, lambda v, r: f"{v / 1024:,.0f} KB"),
]


class UserDialog(QDialog):
    def __init__(self, auth, user: dict | None, parent=None):
        super().__init__(parent)
        self.auth = auth
        self.user = user
        self.setWindowTitle("Edit User" if user else "Add User")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        self.username = QLineEdit(user["username"] if user else "")
        self.username.setEnabled(user is None)
        self.full_name = QLineEdit(user["full_name"] if user else "")
        self.role = QComboBox()
        for role in Role:
            self.role.addItem(role.label, role.value)
        self.role.setCurrentIndex(max(0, self.role.findData(user["role"] if user else Role.OPERATOR.value)))
        self.active = QCheckBox("Active")
        self.active.setChecked(user["is_active"] if user else True)
        form.addRow(label("Username", "FieldLabel"), self.username)
        form.addRow(label("Full name", "FieldLabel"), self.full_name)
        form.addRow(label("Role", "FieldLabel"), self.role)
        form.addRow(label("", "FieldLabel"), self.active)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        self.must_change = QCheckBox("Must change password at first login")
        self.must_change.setChecked(True)
        if user is None:
            form.addRow(label("Password", "FieldLabel"), self.password)
            form.addRow(label("Repeat password", "FieldLabel"), self.repeat)
            form.addRow(label("", "FieldLabel"), self.must_change)
        layout.addLayout(form)
        layout.addWidget(label("Operator: operation screen, history, tank levels. Supervisor: + export, events, "
                               "MES sync, force completion. Admin: everything.", "Hint", wrap=True))
        self.error = label("", "ErrorText", wrap=True)
        layout.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        ok = button("Save", "primary")
        ok.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)

    def _save(self) -> None:
        role = Role(self.role.currentData())
        if self.user is None:
            if self.password.text() != self.repeat.text():
                self.error.setText("Passwords do not match.")
                return
            ok, message = self.auth.create_user(self.username.text(), self.full_name.text(), role,
                                                self.password.text(), self.must_change.isChecked())
        else:
            ok, message = self.auth.update_user(self.user["id"], self.full_name.text(), role, self.active.isChecked())
        if not ok:
            self.error.setText(message)
            return
        self.accept()


class ResetPasswordDialog(QDialog):
    def __init__(self, auth, user: dict, parent=None):
        super().__init__(parent)
        self.auth = auth
        self.user = user
        self.setWindowTitle(f"Reset Password – {user['username']}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        self.must_change = QCheckBox("Must change password at next login")
        self.must_change.setChecked(True)
        form.addRow(label("New password", "FieldLabel"), self.password)
        form.addRow(label("Repeat password", "FieldLabel"), self.repeat)
        form.addRow(label("", "FieldLabel"), self.must_change)
        layout.addLayout(form)
        self.error = label("", "ErrorText", wrap=True)
        layout.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        ok = button("Reset Password", "primary")
        ok.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)

    def _save(self) -> None:
        if self.password.text() != self.repeat.text():
            self.error.setText("Passwords do not match.")
            return
        ok, message = self.auth.admin_set_password(self.user["id"], self.password.text(), self.must_change.isChecked())
        if not ok:
            self.error.setText(message)
            return
        self.accept()


def restart_application() -> None:
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    QProcess.startDetached(sys.executable, args, os.getcwd())
    QApplication.quit()


class AdminPage(BasePage):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.root.addWidget(PageHeader("Administration", "Users, application settings, database and diagnostics."))
        self.tabs = QTabWidget()
        self.tabs.addTab(self._users_tab(), "Users && Roles")
        self.tabs.addTab(self._settings_tab(), "Application Settings")
        self.tabs.addTab(self._database_tab(), "Database && Backup")
        self.tabs.addTab(self._logs_tab(), "Logs")
        self.tabs.addTab(self._diagnostics_tab(), "System Diagnostics")
        self.tabs.currentChanged.connect(lambda _i: self.on_show())
        self.root.addWidget(self.tabs, 1)

    # ================================================================== tabs
    def _users_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        bar = QHBoxLayout()
        bar.addStretch(1)
        add = button("Add User", "primary")
        add.clicked.connect(self._add_user)
        edit = button("Edit")
        edit.clicked.connect(self._edit_user)
        reset = button("Reset Password")
        reset.clicked.connect(self._reset_password)
        for btn in (add, edit, reset):
            bar.addWidget(btn)
        layout.addLayout(bar)
        self.user_model = RecordTableModel(USER_COLUMNS)
        self.user_table, _p = make_table(self.user_model)
        self.user_table.doubleClicked.connect(lambda _i: self._edit_user())
        layout.addWidget(self.user_table, 1)
        return tab

    def _settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        self.app_form = ConfigForm(APP_SECTIONS, columns=2)
        layout.addWidget(self.app_form)
        clock = Card("System Date & Time")
        self.clock_grid = KeyValueGrid(columns=2)
        for key, caption in (("local", "Local time"), ("utc", "UTC time"), ("zone", "Time zone"),
                             ("offset", "UTC offset")):
            self.clock_grid.add(key, caption)
        clock.body.addWidget(self.clock_grid)
        clock.body.addWidget(label(
            "Timestamps are stored in UTC and shown in local time. The IPC system clock should be synchronised "
            "with the plant time server (NTP) - it is managed by Windows, not by this application.", "Hint",
            wrap=True))
        layout.addWidget(clock)
        row = QHBoxLayout()
        row.addStretch(1)
        save = button("Save Settings", "primary")
        save.clicked.connect(self._save_settings)
        row.addWidget(save)
        layout.addLayout(row)
        layout.addStretch(1)
        return scroll_wrap(tab)

    def _database_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        left = QVBoxLayout()
        info = Card("Database")
        self.db_grid = KeyValueGrid()
        for key, caption in (("path", "Database file"), ("size", "Size"), ("schema", "Schema version"),
                             ("integrity", "Integrity check"), ("backups", "Backup folder"),
                             ("last_auto", "Last automatic backup")):
            self.db_grid.add(key, caption)
        info.body.addWidget(self.db_grid)
        row = QHBoxLayout()
        backup = button("Backup Now", "primary")
        backup.clicked.connect(self._backup_now)
        integrity = button("Run Integrity Check")
        integrity.clicked.connect(self._integrity_check)
        open_folder = button("Open Backup Folder")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(
            str(self.ctx.paths.backups_dir))))
        for btn in (backup, integrity, open_folder):
            row.addWidget(btn)
        row.addStretch(1)
        info.body.addLayout(row)
        left.addWidget(info)
        self.backup_form = ConfigForm(BACKUP_SECTIONS)
        left.addWidget(self.backup_form)
        save = button("Save Backup Settings")
        save.clicked.connect(self._save_backup_settings)
        left.addWidget(save)
        left.addStretch(1)
        layout.addLayout(left, 2)

        backups = Card("Backups")
        backups.body.addWidget(MessageBox(
            "A restore replaces ALL current data with the backup. A safety backup of the current database is "
            "taken automatically and the application restarts.", "warn"))
        self.backup_model = RecordTableModel(BACKUP_COLUMNS)
        self.backup_table, _p = make_table(self.backup_model)
        backups.body.addWidget(self.backup_table, 1)
        row = QHBoxLayout()
        restore_selected = button("Restore Selected…", "danger")
        restore_selected.clicked.connect(self._restore_selected)
        restore_file = button("Restore From File…")
        restore_file.clicked.connect(self._restore_file)
        row.addWidget(restore_selected)
        row.addWidget(restore_file)
        row.addStretch(1)
        backups.body.addLayout(row)
        layout.addWidget(backups, 3)
        return tab

    def _logs_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        row = QHBoxLayout()
        self.log_path = label("", "ValueLabel")
        row.addWidget(label("Log file:", "FieldLabel"))
        row.addWidget(self.log_path, 1)
        row.addWidget(label("Level", "FieldLabel"))
        self.log_level = QComboBox()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.log_level.addItem(level, level)
        self.log_level.activated.connect(self._set_log_level)
        row.addWidget(self.log_level)
        open_folder = button("Open Log Folder")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(
            str(self.ctx.paths.logs_dir))))
        refresh = button("Refresh")
        refresh.clicked.connect(self._load_log)
        row.addWidget(open_folder)
        row.addWidget(refresh)
        layout.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 9pt;")
        layout.addWidget(self.log_view, 1)
        return tab

    def _diagnostics_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 9.5pt;")
        layout.addWidget(self.diagnostics, 1)
        row = QHBoxLayout()
        refresh = button("Refresh")
        refresh.clicked.connect(self._load_diagnostics)
        copy = button("Copy to Clipboard")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.diagnostics.toPlainText()))
        row.addWidget(refresh)
        row.addWidget(copy)
        row.addStretch(1)
        layout.addLayout(row)
        return tab

    # ================================================================== page
    def on_show(self) -> None:
        index = self.tabs.currentIndex()
        if index == 0:
            self._load_users()
        elif index == 1:
            self.app_form.load_dataclass(self.ctx.config.app())
            self._load_clock()
        elif index == 2:
            self.backup_form.load_dataclass(self.ctx.config.app())
            self._load_database()
        elif index == 3:
            self._load_log()
        else:
            self._load_diagnostics()

    def on_user_changed(self) -> None:
        self.tabs.setTabVisible(0, self.ctx.auth.has_permission(Permission.MANAGE_USERS))

    def can_leave(self) -> bool:
        if self.app_form.is_dirty() or self.backup_form.is_dirty():
            return confirm(self, "Unsaved Changes", "Discard the unsaved settings?", yes_text="Discard")
        return True

    # ------------------------------------------------------------------ users
    def _load_users(self) -> None:
        self.user_model.set_rows(self.ctx.auth.list_users())

    def _add_user(self) -> None:
        if UserDialog(self.ctx.auth, None, self).exec() == QDialog.DialogCode.Accepted:
            self._load_users()

    def _edit_user(self) -> None:
        user = selected_row(self.user_table)
        if user and UserDialog(self.ctx.auth, user, self).exec() == QDialog.DialogCode.Accepted:
            self._load_users()

    def _reset_password(self) -> None:
        user = selected_row(self.user_table)
        if user and ResetPasswordDialog(self.ctx.auth, user, self).exec() == QDialog.DialogCode.Accepted:
            show_info(self, "Reset Password", f"Password of '{user['username']}' has been reset.")
            self._load_users()

    # ------------------------------------------------------------------ settings
    def _load_clock(self) -> None:
        now = datetime.now().astimezone()
        self.clock_grid.set("local", now.strftime("%Y-%m-%d %H:%M:%S"))
        self.clock_grid.set("utc", utc_now().strftime("%Y-%m-%d %H:%M:%S"))
        self.clock_grid.set("zone", time.tzname[time.localtime().tm_isdst > 0] if time.tzname else "")
        offset = now.utcoffset()
        hours = offset.total_seconds() / 3600 if offset else 0
        self.clock_grid.set("offset", f"UTC{hours:+.2g}")

    def _save_settings(self) -> None:
        cfg = self.app_form.apply_to(self.ctx.config.app())
        if not cfg.station_code.strip() or not cfg.station_code.replace("-", "").replace("_", "").isalnum():
            show_error(self, "Settings", "Station code is required (letters, digits, '-' and '_').")
            return
        changed = self.ctx.config.save("app", cfg, self.username)
        self.app_form.mark_clean()
        show_info(self, "Settings", "Settings saved." if changed else "No changes.")

    def _save_backup_settings(self) -> None:
        cfg = self.backup_form.apply_to(self.ctx.config.app())
        self.ctx.config.save("app", cfg, self.username)
        self.backup_form.mark_clean()
        show_info(self, "Backup Settings", "Saved.")

    # ------------------------------------------------------------------ database
    def _load_database(self) -> None:
        db = self.ctx.db
        g = self.db_grid
        g.set("path", str(db.path))
        g.set("size", f"{db.size_bytes() / 1024 / 1024:,.2f} MB")
        g.set("schema", str(self.ctx.schema_version))
        g.set("backups", str(self.ctx.paths.backups_dir))
        last = self.ctx.backup.last_auto_backup()
        g.set("last_auto", last.strftime("%Y-%m-%d %H:%M") if last else "—")
        self.backup_model.set_rows([
            {"name": b.name, "modified": b.modified.astimezone(), "size": b.size_bytes, "path": str(b.path)}
            for b in self.ctx.backup.list_backups()
        ])

    def _backup_now(self) -> None:
        try:
            path = self.ctx.backup.create_backup("manual")
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Backup", f"Backup failed: {exc}")
            return
        self.ctx.audit.log("DATABASE", "BACKUP", f"Manual backup created: {path.name}")
        self._load_database()
        show_info(self, "Backup", f"Backup created:\n{path}")

    def _integrity_check(self) -> None:
        result = self.ctx.db.integrity_check()
        self.db_grid.set("integrity", result, "ok" if result == "ok" else "ng")

    def _restore(self, path: Path) -> None:
        ok, message = validate_backup_file(path)
        if not ok:
            show_error(self, "Restore", f"The selected file cannot be restored:\n{message}")
            return
        if not confirm(self, "Restore Database",
                       f"Restore '{path.name}' ({message})?\n\nALL current data will be replaced. A safety backup "
                       "of the current database is taken first. The application restarts to complete the restore.",
                       danger=True, yes_text="Restore and Restart"):
            return
        if self.ctx.transactions.is_transaction_open:
            show_error(self, "Restore", "Finish or cancel the current material transaction first.")
            return
        try:
            self.ctx.backup.schedule_restore(path)
        except Exception as exc:  # noqa: BLE001
            show_error(self, "Restore", str(exc))
            return
        self.ctx.audit.log("DATABASE", "RESTORE_SCHEDULED", f"Restore scheduled from {path}")
        restart_application()

    def _restore_selected(self) -> None:
        row = selected_row(self.backup_table)
        if row:
            self._restore(Path(row["path"]))

    def _restore_file(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, "Select backup", str(self.ctx.paths.backups_dir),
                                               "SQLite database (*.db)")
        if path:
            self._restore(Path(path))

    # ------------------------------------------------------------------ logs / diagnostics
    def _load_log(self) -> None:
        self.log_path.setText(str(logging_service.log_file_path() or ""))
        self.log_level.setCurrentIndex(max(0, self.log_level.findData(self.ctx.config.app().log_level)))
        self.log_view.setPlainText(logging_service.tail(400))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _set_log_level(self) -> None:
        level = self.log_level.currentData()
        logging_service.set_level(level)
        cfg = self.ctx.config.app()
        cfg.log_level = level
        self.ctx.config.save("app", cfg, self.username)

    def _load_diagnostics(self) -> None:
        import PySide6
        import sqlalchemy
        from PySide6.QtCore import qVersion

        ctx = self.ctx
        plc_stats = ctx.plc.stats or {}
        counts = ctx.mes.refresh_counts()
        lines = [
            f"Application        : MOBIS – Input Feeder Flow Management {__version__}",
            f"Python             : {sys.version.split()[0]} ({platform.architecture()[0]})",
            f"PySide6 / Qt       : {PySide6.__version__} / {qVersion()}",
            f"SQLAlchemy         : {sqlalchemy.__version__}",
            f"Operating system   : {platform.platform()}",
            f"Computer           : {platform.node()}",
            "",
            f"Data directory     : {ctx.paths.data_dir}",
            f"Database           : {ctx.db.path} ({ctx.db.size_bytes() / 1024:,.0f} KB, schema v{ctx.schema_version})",
            f"Log file           : {logging_service.log_file_path()}",
            "",
            f"Simulation forced  : {ctx.simulate}",
            f"PLC worker         : {'running' if ctx.plc.worker_running else 'stopped'}; "
            f"{'connected' if ctx.plc.connected else 'disconnected'} ({ctx.plc.status_text})",
            f"PLC mode           : {'simulator' if ctx.plc.simulated else 'real PLC'}; heartbeat "
            f"{ctx.plc.heartbeat_state} – {ctx.plc.heartbeat_detail}",
            f"PLC statistics     : {plc_stats}",
            f"Scanner            : mode {ctx.scanner.mode}, state {ctx.scanner.state} – {ctx.scanner.detail}",
            f"MES worker         : {'running' if ctx.mes.worker_running else 'stopped'}; status "
            f"{ctx.mes.status()[1]}; queue {counts}",
            f"Transaction state  : {ctx.transactions.state.value}",
            f"Current user       : {ctx.auth.current_user.display} ({ctx.auth.current_user.role.value})",
            "",
            "Configuration (secrets masked):",
        ]
        for section, values in ctx.config.export_masked().items():
            lines.append(f"  [{section}]")
            lines.extend(f"    {k} = {v!r}" for k, v in values.items())
        self.diagnostics.setPlainText("\n".join(lines))
