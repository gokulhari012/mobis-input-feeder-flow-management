"""Main window: header, permission aware navigation and page hosting."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from mobis import __app_name__
from mobis.core.models import Severity
from mobis.core.permissions import Permission
from mobis.core.process_state_machine import LOADING_STATES
from mobis.resources import icon
from mobis.ui.admin.admin_page import AdminPage
from mobis.ui.bag_mapping.bag_mapping_page import BagMappingPage
from mobis.ui.dashboard.dashboard_page import DashboardPage
from mobis.ui.dialogs import LoginDialog
from mobis.ui.equipment.equipment_page import EquipmentPage
from mobis.ui.history.history_page import HistoryPage
from mobis.ui.inventory.inventory_page import InventoryPage
from mobis.ui.mes.mes_page import MesPage
from mobis.ui.plc.plc_page import PlcPage
from mobis.ui.scanner.scanner_page import ScannerPage
from mobis.ui.widgets.common import confirm, show_info, show_warning

from .navigation import HeaderBar, NavigationSidebar, NavItem

log = logging.getLogger("mobis.ui")

NAV_ITEMS = [
    NavItem("dashboard", "Operation", "dashboard", Permission.OPERATE, DashboardPage, "Production"),
    NavItem("inventory", "Tank Inventory", "tank", Permission.VIEW_INVENTORY, InventoryPage, "Production"),
    NavItem("history", "History & Traceability", "history", Permission.VIEW_HISTORY, HistoryPage, "Production"),
    NavItem("bag_mapping", "Bag Type Mapping", "flow", Permission.MANAGE_BAG_TYPES, BagMappingPage, "Configuration"),
    NavItem("equipment", "Equipment", "equipment", Permission.MANAGE_EQUIPMENT, EquipmentPage, "Configuration"),
    NavItem("scanner", "Barcode Scanner", "barcode", Permission.CONFIGURE_SCANNER, ScannerPage, "Configuration"),
    NavItem("plc", "PLC (SLMP)", "plc", Permission.CONFIGURE_PLC, PlcPage, "Configuration"),
    NavItem("mes", "MES Integration", "cloud", Permission.MES_OPERATIONS, MesPage, "Configuration"),
    NavItem("admin", "Administration", "shield", Permission.MANAGE_SYSTEM, AdminPage, "System"),
]
OPERATOR_PAGES = {"dashboard", "inventory", "history"}


class ActivityFilter(QObject):
    """Resets the inactivity timer of elevated sessions on any user input."""

    def __init__(self, auth):
        super().__init__()
        self._auth = auth

    def eventFilter(self, _obj, event):  # noqa: N802
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            self._auth.touch()
        return False


class MainWindow(QMainWindow):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(__app_name__)
        self.setWindowIcon(icon("app"))
        self.resize(1480, 920)
        self.setMinimumSize(1200, 720)
        self._exit_confirmed = False

        root = QWidget()
        root.setObjectName("AppRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = HeaderBar()
        self.header.login_requested.connect(lambda: self.request_login())
        self.header.logout_requested.connect(lambda: self.ctx.auth.logout())
        outer.addWidget(self.header)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self._items = {item.key: item for item in NAV_ITEMS}
        self.sidebar = NavigationSidebar(NAV_ITEMS)
        self.sidebar.page_requested.connect(self.navigate)
        body.addWidget(self.sidebar)
        self.stack = QStackedWidget()
        self.stack.setObjectName("PageStack")
        self._pages = {}
        for item in NAV_ITEMS:
            page = item.factory(ctx)
            self._pages[item.key] = page
            self.stack.addWidget(page)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Ready")
        self._current = None

        self._activity = ActivityFilter(ctx.auth)
        QApplication.instance().installEventFilter(self._activity)

        ctx.scanner.barcode_scanned.connect(self._on_barcode)
        ctx.auth.user_changed.connect(self._on_user_changed)
        ctx.auth.session_expired.connect(self._on_session_expired)
        ctx.events.event_logged.connect(self._on_event)
        ctx.config.changed.connect(self._on_config_changed)
        for signal in (ctx.plc.status_changed, ctx.scanner.status_changed, ctx.mes.status_changed):
            signal.connect(self._update_pills)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self.header.update_clock)
        self._clock.start(1000)
        self._session_timer = QTimer(self)
        self._session_timer.timeout.connect(self.ctx.auth.check_timeout)
        self._session_timer.start(10_000)
        self._pill_timer = QTimer(self)
        self._pill_timer.timeout.connect(self._update_pills)
        self._pill_timer.start(3000)
        self._backup_timer = QTimer(self)
        self._backup_timer.timeout.connect(self._auto_backup)
        self._backup_timer.start(10 * 60 * 1000)
        QTimer.singleShot(60_000, self._auto_backup)

        self._on_config_changed("app")
        self._on_user_changed()
        self.navigate("dashboard")

    # ================================================================== startup
    def show_initial(self, fullscreen: bool = False) -> None:
        if fullscreen or self.ctx.config.app().kiosk_fullscreen:
            self.showFullScreen()
        else:
            self.showMaximized()

    def handle_startup(self) -> None:
        messages = list(self.ctx.startup_messages)
        if any("new database" in m for m in messages):
            messages.append("Default administrator: user 'admin', password 'admin' (a new password is required at "
                            "first login). Demo bag types are configured for testing.")
        if messages:
            show_info(self, "MOBIS – Start-up", "\n\n".join(messages))
        if self.ctx.config.app().require_operator_login:
            self.require_login()

    # ================================================================== navigation
    def navigate(self, key: str, force: bool = False) -> bool:
        item = self._items.get(key)
        if item is None:
            return False
        auth = self.ctx.auth
        if not auth.has_permission(item.permission):
            if not self.request_login(f"'{item.title}' requires a supervisor or administrator login."):
                self.sidebar.set_current(self._current)
                return False
            if not auth.has_permission(item.permission):
                show_warning(self, "Access Denied", f"Your account does not have access to '{item.title}'.")
                self.sidebar.set_current(self._current)
                return False
        current = self._pages.get(self._current)
        if current is not None and key != self._current:
            if not force and not current.can_leave():
                self.sidebar.set_current(self._current)
                return False
            current.on_hide()
        page = self._pages[key]
        changed = key != self._current
        self._current = key
        self.stack.setCurrentWidget(page)
        self.sidebar.set_current(key)
        if changed or force:
            page.on_show()
        return True

    def request_login(self, message: str = "") -> bool:
        return LoginDialog(self.ctx.auth, self, message).exec() == LoginDialog.DialogCode.Accepted

    def require_login(self) -> None:
        while self.ctx.auth.current_user.is_station:
            dialog = LoginDialog(self.ctx.auth, self, "Operator login is required.", allow_cancel=False)
            if dialog.exec() != LoginDialog.DialogCode.Accepted:
                return  # application is quitting (Exit button)

    # ================================================================== events
    def _on_barcode(self, barcode: str, source: str) -> None:
        if self._current != "dashboard":
            page = self._pages.get(self._current)
            if page is not None and page.has_unsaved_changes():
                self.statusBar().showMessage(
                    f"Scan '{barcode}' processed - open the Operation screen to see the result.", 10_000)
            else:
                self.navigate("dashboard", force=True)
        self.ctx.transactions.process_scan(barcode, source)

    def _on_user_changed(self) -> None:
        auth = self.ctx.auth
        user = auth.current_user
        self.header.set_user(user.display, user.role.value, not user.is_station)
        for key, item in self._items.items():
            self.sidebar.set_locked(key, not auth.has_permission(item.permission))
        for page in self._pages.values():
            page.on_user_changed()
        if self._current and not auth.has_permission(self._items[self._current].permission):
            self.navigate("dashboard", force=True)

    def _on_session_expired(self) -> None:
        self.statusBar().showMessage("Supervisor / administrator session ended after inactivity.", 15_000)
        if self.ctx.config.app().require_operator_login:
            QTimer.singleShot(0, self.require_login)

    def _on_event(self, severity: str, source: str, message: str) -> None:
        if severity in (Severity.ERROR.value, Severity.CRITICAL.value):
            self.statusBar().showMessage(f"{source}: {message}", 15_000)

    def _on_config_changed(self, section: str) -> None:
        if section == "app":
            app = self.ctx.config.app()
            self.header.set_station(app.station_code, app.station_name, app.plant_name)
        self._update_pills()

    def _update_pills(self) -> None:
        ctx = self.ctx
        plc = ctx.plc
        cfg = plc.config
        if not cfg.enabled:
            self.header.pill_plc.set_state("neutral", "Disabled")
        elif plc.connected:
            hb_ok = plc.heartbeat_state in ("OK", "NOT_CONFIGURED")
            text = "Simulator" if plc.simulated else "Connected"
            level = ("info" if plc.simulated else "ok") if hb_ok else "warn"
            self.header.pill_plc.set_state(level, text, f"Heartbeat: {plc.heartbeat_state} – {plc.heartbeat_detail}")
        else:
            self.header.pill_plc.set_state("ng", "Offline", plc.status_text)
        level, text = ctx.scanner.status()
        self.header.pill_scanner.set_state(level, text, ctx.scanner.detail)
        level, text = ctx.mes.status()
        counts = ctx.mes.counts or {}
        pending = counts.get("PENDING", 0) + counts.get("SYNCING", 0)
        self.header.pill_mes.set_state(level, f"{text} · {pending} pending", ctx.mes.last_error)
        ok, detail = ctx.db.health_check()
        self.header.pill_db.set_state("ok" if ok else "ng", "OK" if ok else "ERROR", detail)
        self.header.sim_badge.setVisible(plc.simulated or ctx.scanner.simulation_enabled)

    def _auto_backup(self) -> None:
        app = self.ctx.config.app()
        if not app.auto_backup_enabled:
            return
        last = self.ctx.backup.last_auto_backup()
        if last is not None and (datetime.now() - last).total_seconds() < app.auto_backup_interval_h * 3600:
            return
        try:
            path = self.ctx.backup.create_backup("auto")
            removed = self.ctx.backup.prune_auto_backups(app.backup_retention_count)
            self.ctx.events.log(Severity.INFO, "DATABASE", "AUTO_BACKUP",
                                f"Automatic backup {path.name} created ({removed} old backup(s) removed)")
        except Exception as exc:  # noqa: BLE001
            log.exception("Automatic backup failed")
            self.ctx.events.log(Severity.ERROR, "DATABASE", "AUTO_BACKUP_FAILED", str(exc))

    # ================================================================== exit
    def closeEvent(self, event):  # noqa: N802
        if self._exit_confirmed:
            event.accept()
            return
        tm = self.ctx.transactions
        if tm.is_transaction_open and tm.state in LOADING_STATES:
            text = ("A material loading is in progress.\n\nExiting revokes the PLC loading authorisation and cancels "
                    "the transaction (tank quantity NOT updated). Exit anyway?")
            danger = True
        elif tm.is_transaction_open:
            text = "A transaction is in progress and will be cancelled. Exit anyway?"
            danger = True
        else:
            text = "Exit MOBIS – Input Feeder Flow Management?"
            danger = False
        if not confirm(self, "Exit", text, danger=danger, yes_text="Exit"):
            event.ignore()
            return
        if tm.is_transaction_open:
            tm.cancel("Application closed")
        self._exit_confirmed = True
        event.accept()
