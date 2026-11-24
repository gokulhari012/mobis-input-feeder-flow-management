"""Application entry point: argument parsing, bootstrap and shutdown."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from mobis import __app_name__, __organization__, __short_name__, __version__
from mobis.app.styles import apply_theme
from mobis.resources import icon
from mobis.services.backup_service import BackupService
from mobis.services.logging_service import set_level, setup_logging
from mobis.services.paths import AppPaths
from mobis.ui.widgets.common import font

log = logging.getLogger("mobis.app")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mobis", description=__app_name__)
    parser.add_argument("--data-dir", help="Directory for the database, logs, backups and exports "
                                           "(default: %%PROGRAMDATA%%\\MOBIS or MOBIS_DATA_DIR)")
    parser.add_argument("--simulate", action="store_true",
                        help="Force PLC and scanner simulation (test without hardware)")
    parser.add_argument("--no-demo-data", action="store_true",
                        help="Do not create demo bag types when a new database is created")
    parser.add_argument("--fullscreen", action="store_true", help="Start in full screen (kiosk) mode")
    args, _unknown = parser.parse_known_args(argv)  # Qt options are left for QApplication
    return args


def run(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        paths = AppPaths.resolve(args.data_dir)
    except PermissionError as exc:
        print(f"MOBIS cannot start: {exc}", file=sys.stderr)
        return 2
    log_file = setup_logging(paths.logs_dir)
    log.info("Starting %s %s (data directory: %s)", __app_name__, __version__, paths.data_dir)

    QApplication.setApplicationName(__short_name__)
    QApplication.setOrganizationName(__organization__)
    QApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv)
    apply_theme(app)  # light theme even when Windows runs in dark mode
    app.setFont(font(10))
    app.setWindowIcon(icon("app"))

    lock = QLockFile(str(paths.data_dir / "mobis.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.warning(None, __app_name__,
                            "MOBIS is already running on this computer (or the data directory is in use).")
        return 1

    from mobis.app.context import build_context
    from mobis.app.main_window import MainWindow

    try:
        restore_message = BackupService.apply_pending_restore(paths)
        ctx = build_context(paths, simulate=args.simulate, demo_data=not args.no_demo_data)
    except Exception as exc:  # noqa: BLE001
        log.exception("Start-up failed")
        QMessageBox.critical(None, f"{__app_name__} – Start-up error",
                             f"The application could not start:\n\n{exc}\n\nLog file: {log_file}")
        lock.unlock()
        return 1
    set_level(ctx.config.app().log_level)
    if restore_message:
        ctx.startup_messages.insert(0, restore_message)
        ctx.audit.log("DATABASE", "RESTORED", restore_message, username="system")

    window = MainWindow(ctx)
    ctx.start_services()
    window.show_initial(fullscreen=args.fullscreen)
    window.handle_startup()
    try:
        exit_code = app.exec()
    finally:
        log.info("Shutting down")
        ctx.shutdown()
        lock.unlock()
    return exit_code
