"""Static resources (icons)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QIcon

ICON_DIR = Path(__file__).resolve().parent / "icons"


@lru_cache(maxsize=None)
def icon(name: str) -> QIcon:
    path = ICON_DIR / f"{name}.svg"
    return QIcon(str(path)) if path.exists() else QIcon()
