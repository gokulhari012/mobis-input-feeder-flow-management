"""Sidebar navigation and header bar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from mobis import __app_name__, __version__
from mobis.core.permissions import Permission
from mobis.resources import icon
from mobis.ui.widgets.common import ElidedLabel, StatusPill, button, label, repolish


@dataclass(frozen=True)
class NavItem:
    key: str
    title: str
    icon: str
    permission: Permission
    factory: Callable
    section: str = ""


class NavigationSidebar(QFrame):
    page_requested = Signal(str)

    def __init__(self, items: list[NavItem], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(236)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        section = None
        for item in items:
            if item.section and item.section != section:
                section = item.section
                layout.addWidget(label(section.upper(), "SidebarSection"))
            btn = QPushButton(f"  {item.title.replace('&', '&&')}")  # '&' would become a mnemonic
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setIcon(icon(item.icon))
            btn.setIconSize(QSize(20, 20))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _checked=False, key=item.key: self.page_requested.emit(key))
            self._group.addButton(btn)
            self._buttons[item.key] = btn
            layout.addWidget(btn)
        layout.addStretch(1)
        footer = label(f"MOBIS IFFM  ·  Version {__version__}", "SidebarFooter")
        layout.addWidget(footer)

    def set_current(self, key: str | None) -> None:
        if key in self._buttons:
            self._buttons[key].setChecked(True)

    def set_locked(self, key: str, locked: bool) -> None:
        btn = self._buttons.get(key)
        if btn is None:
            return
        btn.setProperty("locked", "true" if locked else "false")
        btn.setToolTip("Requires supervisor / administrator login" if locked else "")
        repolish(btn)


class HeaderBar(QFrame):
    login_requested = Signal()
    logout_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("HeaderBar")
        self.setFixedHeight(64)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icon("app").pixmap(38, 38))
        layout.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(ElidedLabel(__app_name__, "AppTitle"))
        self.subtitle = ElidedLabel("", "AppSubtitle")
        titles.addWidget(self.subtitle)
        layout.addLayout(titles, 1)
        self.sim_badge = label("SIMULATION", "SimBadge")
        self.sim_badge.setToolTip("PLC and / or scanner simulation is active - not connected to real hardware")
        layout.addWidget(self.sim_badge)
        self.pill_plc = StatusPill("PLC")
        self.pill_scanner = StatusPill("Scanner")
        self.pill_mes = StatusPill("MES")
        self.pill_db = StatusPill("DB")
        for pill in (self.pill_plc, self.pill_scanner, self.pill_mes, self.pill_db):
            layout.addWidget(pill)
        layout.addSpacing(12)
        user_icon = QLabel()
        user_icon.setPixmap(icon("user").pixmap(20, 20))
        layout.addWidget(user_icon)
        self.user_label = label("", "UserLabel")
        layout.addWidget(self.user_label)
        self.login_button = button("Login", focusable=False)
        self.login_button.clicked.connect(self._on_login_button)
        layout.addWidget(self.login_button)
        layout.addSpacing(12)
        self.clock = label("", "Clock")
        layout.addWidget(self.clock)
        self._logged_in = False
        self.update_clock()

    def set_station(self, station_code: str, station_name: str, plant: str) -> None:
        parts = [f"Station {station_code}", station_name] + ([plant] if plant else [])
        self.subtitle.setText("  ·  ".join(p for p in parts if p))

    def set_user(self, display: str, role: str, logged_in: bool) -> None:
        self._logged_in = logged_in
        self.user_label.setText(f"{display}  ({role.title()})" if logged_in else display)
        self.login_button.setText("Logout" if logged_in else "Login")

    def _on_login_button(self) -> None:
        if self._logged_in:
            self.logout_requested.emit()
        else:
            self.login_requested.emit()

    def update_clock(self) -> None:
        self.clock.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
