"""Audit log (administrative / configuration changes) and system event log."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal
from sqlalchemy.orm import Session

from mobis.core.models import Severity
from mobis.database.database import Database
from mobis.database.repositories import Repositories

log = logging.getLogger("mobis.audit")

_LEVELS = {
    Severity.DEBUG: logging.DEBUG,
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}


def _to_json(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


class AuditService:
    """Records who changed what (never raises - failures are logged)."""

    def __init__(self, db: Database, repos: Repositories, user_provider: Callable[[], str] = lambda: ""):
        self._db = db
        self._repo = repos.audit
        self._user_provider = user_provider

    def set_user_provider(self, provider: Callable[[], str]) -> None:
        self._user_provider = provider

    def log(
        self,
        category: str,
        action: str,
        details: str = "",
        entity: str = "",
        entity_id: Any = "",
        old: Any = None,
        new: Any = None,
        username: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> None:
        user = username if username is not None else (self._user_provider() or "")
        fields = dict(
            username=user,
            category=category,
            action=action,
            entity=entity,
            entity_id=str(entity_id if entity_id is not None else ""),
            details=details,
            old_value=_to_json(old),
            new_value=_to_json(new),
        )
        log.info("AUDIT %s/%s by '%s': %s", category, action, user, details)
        try:
            if session is not None:
                self._repo.add_audit(session, **fields)
            else:
                with self._db.session() as s:
                    self._repo.add_audit(s, **fields)
        except Exception:  # noqa: BLE001
            log.exception("Failed to write audit log entry: %s", details)
            if session is not None:
                raise


class EventService(QObject):
    """Operational events (PLC, scanner, MES, validation).  Thread safe."""

    event_logged = Signal(str, str, str)  # severity, source, message

    def __init__(self, db: Database, repos: Repositories):
        super().__init__()
        self._db = db
        self._repo = repos.audit

    def log(
        self,
        severity: Severity | str,
        source: str,
        event_type: str,
        message: str,
        details: str = "",
        transaction_id: Optional[int] = None,
    ) -> None:
        severity = Severity(severity)
        logging.getLogger(f"mobis.{source.lower()}").log(_LEVELS[severity], "%s: %s", event_type, message)
        try:
            with self._db.session() as s:
                self._repo.add_event(
                    s,
                    severity=severity.value,
                    source=source,
                    event_type=event_type,
                    message=message[:2000],
                    details=details[:4000],
                    transaction_id=transaction_id,
                )
        except Exception:  # noqa: BLE001
            log.exception("Failed to store system event %s: %s", event_type, message)
        self.event_logged.emit(severity.value, source, message)
