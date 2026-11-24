"""Light industrial theme: neutral background, dark text, green OK, red NG,
orange warnings, blue information."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

FONT_FAMILY = "Segoe UI"

#: Strong colours (banners, primary buttons): level -> (foreground, background)
LEVELS = {
    "ok": ("#FFFFFF", "#15803D"),
    "ng": ("#FFFFFF", "#B91C1C"),
    "warn": ("#FFFFFF", "#C2410C"),
    "info": ("#FFFFFF", "#1D4ED8"),
    "neutral": ("#1E2833", "#E3E7EC"),
}
#: Soft colours (chips, pills, table cells): level -> (foreground, background)
SOFT = {
    "ok": ("#14532D", "#DCFCE7"),
    "ng": ("#7F1D1D", "#FEE2E2"),
    "warn": ("#7C2D12", "#FFEDD5"),
    "info": ("#1E3A8A", "#DBEAFE"),
    "neutral": ("#374151", "#EEF0F3"),
}
DOT = {"ok": "#16A34A", "ng": "#DC2626", "warn": "#EA580C", "info": "#2563EB", "neutral": "#9CA3AF"}
RESULT_LEVEL = {"OK": "ok", "NG": "ng", "NA": "neutral", "PENDING": "neutral", "IGNORED": "warn"}
STATUS_LEVEL = {
    "COMPLETED": "ok", "IN_PROGRESS": "info", "REJECTED": "ng", "CANCELLED": "neutral", "INTERRUPTED": "warn",
    "SYNCED": "ok", "PENDING": "warn", "SYNCING": "info", "FAILED": "ng", "NOT_QUEUED": "neutral",
    "INFO": "info", "WARNING": "warn", "ERROR": "ng", "CRITICAL": "ng", "DEBUG": "neutral",
}

BG = "#EEF1F5"
SURFACE = "#FFFFFF"
BORDER = "#D5DBE3"
TEXT = "#1E2833"
MUTED = "#5B6776"
PRIMARY = "#1D4ED8"


def light_palette() -> QPalette:
    """Explicit light palette.

    Without it, Qt follows the Windows light / dark setting: in dark mode every
    colour not set by the style sheet (e.g. the item text of drop-down lists)
    becomes white and disappears on the light backgrounds of this theme.
    """
    palette = QPalette()
    colours = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: "#F7F9FC",
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.PlaceholderText: "#8A95A3",
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Highlight: "#BFDBFE",
        QPalette.ColorRole.HighlightedText: TEXT,
        QPalette.ColorRole.ToolTipBase: TEXT,
        QPalette.ColorRole.ToolTipText: "#FFFFFF",
        QPalette.ColorRole.Link: PRIMARY,
        QPalette.ColorRole.LinkVisited: "#1E40AF",
        QPalette.ColorRole.Light: "#FFFFFF",
        QPalette.ColorRole.Midlight: "#E3E8EF",
        QPalette.ColorRole.Mid: "#C3CBD6",
        QPalette.ColorRole.Dark: "#9AA5B1",
        QPalette.ColorRole.Shadow: "#6B7280",
    }
    if hasattr(QPalette.ColorRole, "Accent"):  # Qt >= 6.6
        colours[QPalette.ColorRole.Accent] = PRIMARY
    for role, value in colours.items():
        palette.setColor(QPalette.ColorGroup.All, role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#9AA5B1"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor("#F1F3F6"))
    return palette


def apply_theme(app) -> None:
    """Apply the light industrial theme, independent of the Windows light / dark mode."""
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):  # Qt >= 6.8: also keeps native title bars light
        hints.setColorScheme(Qt.ColorScheme.Light)
    app.setStyle("Fusion")  # predictable style-sheet rendering on every Windows version
    app.setPalette(light_palette())  # after setStyle(), which resets the palette
    app.setStyleSheet(stylesheet())


def stylesheet() -> str:
    from mobis.resources import ICON_DIR

    down = (ICON_DIR / "chevron-down.svg").as_posix()
    up = (ICON_DIR / "chevron-up.svg").as_posix()
    return _BASE_QSS + f"""
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; width: 24px; border: none; }}
QComboBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}
QDateEdit {{ padding-right: 26px; }}
QDateEdit::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; width: 24px; border: none; }}
QDateEdit::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; border: none; border-left: 1px solid #E3E8EF; border-top-right-radius: 4px; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; border-left: 1px solid #E3E8EF; border-bottom-right-radius: 4px; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: #EEF2F7; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{up}"); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
"""


_BASE_QSS = f"""
QMainWindow, QWidget#AppRoot, QStackedWidget#PageStack, QWidget#Page {{ background: {BG}; }}
QDialog {{ background: {BG}; }}
QFrame#HeaderBar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QLabel#AppTitle {{ font-size: 15pt; font-weight: 600; color: {TEXT}; }}
QLabel#AppSubtitle {{ color: {MUTED}; }}
QLabel#Clock {{ font-size: 12pt; font-weight: 600; color: {TEXT}; }}
QLabel#UserLabel {{ font-weight: 600; color: {TEXT}; }}
QLabel#SimBadge {{ background: #FFEDD5; color: #7C2D12; border: 1px solid #FDBA74; border-radius: 4px;
                   padding: 2px 8px; font-weight: 600; }}
QFrame#Sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QPushButton#NavButton {{ text-align: left; padding: 11px 16px; border: none; border-left: 4px solid transparent;
                         border-radius: 0px; color: #334155; background: transparent; font-size: 11pt; }}
QPushButton#NavButton:hover {{ background: #F1F5F9; }}
QPushButton#NavButton:checked {{ background: #E8EFFD; border-left: 4px solid {PRIMARY}; color: {PRIMARY};
                                 font-weight: 600; }}
QPushButton#NavButton[locked="true"] {{ color: #94A3B8; }}
QLabel#SidebarSection {{ color: #8A95A3; font-size: 8pt; font-weight: 600; padding: 12px 18px 4px 18px; }}
QLabel#SidebarFooter {{ color: #8A95A3; font-size: 8pt; padding: 10px 18px; }}
QFrame#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#CardTitle {{ font-size: 11pt; font-weight: 600; color: #334155; }}
QLabel#PageTitle {{ font-size: 17pt; font-weight: 600; color: {TEXT}; }}
QLabel#PageSubtitle {{ color: {MUTED}; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Hint {{ color: {MUTED}; font-size: 9pt; }}
QLabel#ErrorText {{ color: #B91C1C; }}
QLabel#FieldLabel {{ color: #475569; }}
QLabel#ValueLabel {{ font-weight: 600; color: {TEXT}; }}
QLabel#BigValue {{ font-size: 18pt; font-weight: 600; color: {TEXT}; }}
QLabel#SectionLabel {{ font-size: 10pt; font-weight: 600; color: #475569; }}
QFrame#InfoBox {{ background: #EFF6FF; border: 1px solid #BFDBFE; border-radius: 6px; }}
QFrame#WarnBox {{ background: #FFF7ED; border: 1px solid #FED7AA; border-radius: 6px; }}
QPushButton {{ background: {SURFACE}; border: 1px solid #C3CBD6; border-radius: 4px; padding: 6px 14px;
               color: {TEXT}; min-height: 20px; }}
QPushButton:hover {{ background: #F3F6FA; border-color: #A9B4C2; }}
QPushButton:pressed {{ background: #E6EBF1; }}
QPushButton:disabled {{ color: #9AA5B1; background: #F4F6F8; border-color: #DDE2E8; }}
QPushButton[kind="primary"] {{ background: {PRIMARY}; border-color: {PRIMARY}; color: #FFFFFF; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: #1E40AF; }}
QPushButton[kind="success"] {{ background: #15803D; border-color: #15803D; color: #FFFFFF; font-weight: 600; }}
QPushButton[kind="success"]:hover {{ background: #166534; }}
QPushButton[kind="danger"] {{ background: #B91C1C; border-color: #B91C1C; color: #FFFFFF; font-weight: 600; }}
QPushButton[kind="danger"]:hover {{ background: #991B1B; }}
QPushButton[kind="warning"] {{ background: #C2410C; border-color: #C2410C; color: #FFFFFF; font-weight: 600; }}
QPushButton[kind="warning"]:hover {{ background: #9A3412; }}
QPushButton[kind="primary"]:disabled, QPushButton[kind="success"]:disabled,
QPushButton[kind="danger"]:disabled, QPushButton[kind="warning"]:disabled {{
    background: #C3CBD5; border-color: #C3CBD5; color: #F4F6F8; }}
QPushButton[size="large"] {{ font-size: 12pt; padding: 10px 20px; min-height: 28px; }}
QPushButton[flat="true"] {{ border: none; background: transparent; color: {PRIMARY}; padding: 4px 8px; }}
QPushButton[flat="true"]:hover {{ background: #EEF2FF; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {SURFACE}; border: 1px solid #C3CBD6; border-radius: 4px; padding: 4px 6px; color: {TEXT};
    selection-background-color: #BFDBFE; selection-color: {TEXT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {PRIMARY}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QPlainTextEdit:disabled {{
    background: #F1F3F6; color: #8A95A3; }}
QLineEdit:read-only {{ background: #F7F9FB; }}
QLineEdit[invalid="true"] {{ border: 1px solid #B91C1C; background: #FEF2F2; }}
QLineEdit#ScanInput {{ font-size: 22pt; font-weight: 600; padding: 10px 14px; border: 2px solid {PRIMARY};
                       border-radius: 6px; background: #FFFFFF; }}
QLineEdit#ScanInput:focus {{ border: 2px solid #15803D; background: #F7FEF9; }}
QComboBox#BagTypeCombo {{ font-size: 14pt; padding: 6px 10px; min-height: 34px; }}
QComboBox {{ combobox-popup: 0; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; color: {TEXT}; border: 1px solid #C3CBD6; outline: 0;
                               selection-background-color: #DBEAFE; selection-color: {TEXT}; }}
QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 0px 8px; color: {TEXT}; }}
QComboBox QAbstractItemView::item:selected, QComboBox QAbstractItemView::item:hover {{
    background: #DBEAFE; color: {TEXT}; }}
QComboBox#BagTypeCombo QAbstractItemView {{ font-size: 13pt; }}
QComboBox#BagTypeCombo QAbstractItemView::item {{ min-height: 40px; }}
QMenu {{ background: {SURFACE}; color: {TEXT}; border: 1px solid #C3CBD6; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; }}
QMenu::item:selected {{ background: #DBEAFE; color: {TEXT}; }}
QMenu::item:disabled {{ color: #9AA5B1; }}
QCalendarWidget QAbstractItemView {{ color: {TEXT}; selection-background-color: #DBEAFE; selection-color: {TEXT}; }}
QCheckBox {{ spacing: 8px; color: {TEXT}; }}
QTableView, QTableWidget {{ background: {SURFACE}; alternate-background-color: #F7F9FC; gridline-color: #E3E8EF;
                            border: 1px solid {BORDER}; border-radius: 4px; selection-background-color: #DBEAFE;
                            selection-color: {TEXT}; color: {TEXT}; }}
QHeaderView::section {{ background: #F1F4F8; color: #334155; border: none; border-bottom: 1px solid {BORDER};
                        border-right: 1px solid #E3E8EF; padding: 6px 8px; font-weight: 600; }}
QTableWidget#StageTable {{ font-size: 13pt; border: none; }}
QTableWidget#StageTable QHeaderView::section {{ font-size: 10pt; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {SURFACE}; border-radius: 4px; top: -1px; }}
QTabBar::tab {{ background: #E7EBF0; color: #475569; border: 1px solid {BORDER}; padding: 7px 16px; margin-right: 2px;
                border-top-left-radius: 4px; border-top-right-radius: 4px; }}
QTabBar::tab:selected {{ background: {SURFACE}; color: {TEXT}; border-bottom-color: {SURFACE}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ background: #F1F4F8; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget#ScrollContent {{ background: transparent; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: #475569; }}
QToolTip {{ background: {TEXT}; color: #FFFFFF; border: none; padding: 6px; }}
QProgressBar {{ border: 1px solid #C3CBD6; border-radius: 4px; background: #EEF1F5; text-align: center;
                min-height: 18px; color: {TEXT}; }}
QProgressBar::chunk {{ background: #3B82F6; border-radius: 3px; }}
QProgressBar[level="warn"]::chunk {{ background: #F97316; }}
QProgressBar[level="ng"]::chunk {{ background: #DC2626; }}
QSplitter::handle {{ background: {BORDER}; }}
QListWidget {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; }}
"""
