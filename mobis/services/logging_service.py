"""Persistent application logging (rotating files + console)."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

LOG_FILE_NAME = "mobis.log"
_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(threadName)-14s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_log_file: Optional[Path] = None


def setup_logging(log_dir: Path, level: str = "INFO") -> Path:
    global _log_file
    log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = log_dir / LOG_FILE_NAME
    formatter = logging.Formatter(_FORMAT, _DATE_FORMAT)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = RotatingFileHandler(_log_file, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if sys.stderr is not None:  # None when started without a console (pythonw / packaged GUI build)
        try:
            sys.stderr.reconfigure(errors="backslashreplace")  # never fail on non-ASCII text in a pipe
        except (AttributeError, ValueError):
            pass
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)
    set_level(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("mobis").critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        logging.getLogger("mobis").critical(
            "Unhandled exception in thread %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    return _log_file


def set_level(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))


def log_file_path() -> Optional[Path]:
    return _log_file


def tail(max_lines: int = 300) -> str:
    if _log_file is None or not _log_file.exists():
        return ""
    with _log_file.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 256 * 1024))
        data = handle.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-max_lines:])
