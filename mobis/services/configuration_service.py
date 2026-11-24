"""Persistent, typed application configuration.

Each section is a dataclass from :mod:`mobis.core.config_models`, stored
field-by-field (JSON values) in its own table.  Changes are audited (secrets
masked) and announced through the ``changed`` signal so that running workers
can apply them without restarting the application.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import fields
from typing import Any

from PySide6.QtCore import QObject, Signal

from mobis.core.config_models import AppSettings, BarcodeFormatConfig, MesConfig, PlcConfig, ScannerConfig
from mobis.database.database import Database
from mobis.database.models import AppSettingRow, MesConfigRow, PlcConfigRow, ScannerConfigRow
from mobis.database.repositories import Repositories

from .audit_service import AuditService

log = logging.getLogger("mobis.config")

SECTIONS: dict[str, tuple[type, type, str]] = {
    "plc": (PlcConfig, PlcConfigRow, ""),
    "scanner": (ScannerConfig, ScannerConfigRow, ""),
    "mes": (MesConfig, MesConfigRow, ""),
    "app": (AppSettings, AppSettingRow, "app."),
    "barcode": (BarcodeFormatConfig, AppSettingRow, "barcode."),
}
SECRET_FIELDS = {"mes": {"api_token", "password"}}


def coerce(value: Any, default: Any) -> Any:
    """Convert a stored JSON value to the type of the field default."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return "" if value is None else str(value)


class ConfigurationService(QObject):
    changed = Signal(str)  # section name

    def __init__(self, db: Database, repos: Repositories, audit: AuditService | None = None):
        super().__init__()
        self._db = db
        self._repo = repos.config
        self._audit = audit
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------ access
    def get(self, section: str) -> Any:
        if section not in self._cache:
            self._cache[section] = self._load(section)
        return copy.deepcopy(self._cache[section])

    def plc(self) -> PlcConfig:
        return self.get("plc")

    def scanner(self) -> ScannerConfig:
        return self.get("scanner")

    def mes(self) -> MesConfig:
        return self.get("mes")

    def app(self) -> AppSettings:
        return self.get("app")

    def barcode(self) -> BarcodeFormatConfig:
        return self.get("barcode")

    def reload(self) -> None:
        self._cache.clear()

    def _load(self, section: str) -> Any:
        cls, model, prefix = SECTIONS[section]
        instance = cls()
        try:
            with self._db.read_session() as s:
                stored = self._repo.load(s, model, prefix)
        except Exception:  # noqa: BLE001
            log.exception("Could not load %s configuration; using defaults", section)
            return instance
        for f in fields(cls):
            if f.name not in stored:
                continue
            default = getattr(instance, f.name)
            try:
                setattr(instance, f.name, coerce(json.loads(stored[f.name]), default))
            except (ValueError, TypeError):
                log.warning("Ignoring invalid stored value for %s.%s", section, f.name)
        return instance

    # ------------------------------------------------------------------ update
    def save(self, section: str, config: Any, username: str = "") -> list[str]:
        cls, model, prefix = SECTIONS[section]
        if not isinstance(config, cls):
            raise TypeError(f"Expected {cls.__name__} for section '{section}'")
        current = self.get(section)
        changes = {
            f.name: (getattr(current, f.name), getattr(config, f.name))
            for f in fields(cls)
            if getattr(current, f.name) != getattr(config, f.name)
        }
        if not changes:
            return []
        values = {name: json.dumps(new) for name, (_old, new) in changes.items()}
        secrets = SECRET_FIELDS.get(section, set())
        with self._db.session() as s:
            self._repo.save(s, model, prefix, values, username)
            if self._audit is not None:
                self._audit.log(
                    "CONFIG",
                    "UPDATE",
                    details=f"{section.upper()} configuration changed: {', '.join(sorted(changes))}",
                    entity=section,
                    old={k: ("***" if k in secrets else v[0]) for k, v in changes.items()},
                    new={k: ("***" if k in secrets else v[1]) for k, v in changes.items()},
                    username=username,
                    session=s,
                )
        self._cache[section] = copy.deepcopy(config)
        self.changed.emit(section)
        return sorted(changes)

    def export_masked(self) -> dict[str, dict[str, Any]]:
        """All sections as dictionaries with secrets masked (diagnostics)."""
        result = {}
        for section, (cls, _model, _prefix) in SECTIONS.items():
            cfg = self.get(section)
            secrets = SECRET_FIELDS.get(section, set())
            result[section] = {
                f.name: ("***" if f.name in secrets and getattr(cfg, f.name) else getattr(cfg, f.name))
                for f in fields(cls)
            }
        return result
