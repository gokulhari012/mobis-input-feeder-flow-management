from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from mobis.database.models import AuditLogRow, SystemEventRow
from mobis.utils.timeutil import utc_now


class AuditRepository:
    def add_audit(self, s: Session, **fields: Any) -> AuditLogRow:
        fields.setdefault("occurred_at", utc_now())
        row = AuditLogRow(**fields)
        s.add(row)
        s.flush()
        return row

    def add_event(self, s: Session, **fields: Any) -> SystemEventRow:
        fields.setdefault("occurred_at", utc_now())
        row = SystemEventRow(**fields)
        s.add(row)
        s.flush()
        return row

    def list_audit(
        self,
        s: Session,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        category: str = "",
        text: str = "",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        query = select(AuditLogRow)
        if date_from:
            query = query.where(AuditLogRow.occurred_at >= date_from)
        if date_to:
            query = query.where(AuditLogRow.occurred_at < date_to)
        if category:
            query = query.where(AuditLogRow.category == category)
        if text.strip():
            t = text.strip()
            query = query.where(
                or_(
                    AuditLogRow.details.contains(t, autoescape=True),
                    AuditLogRow.username.contains(t, autoescape=True),
                    AuditLogRow.entity_id.contains(t, autoescape=True),
                )
            )
        query = query.order_by(AuditLogRow.occurred_at.desc(), AuditLogRow.id.desc()).limit(limit)
        return [row.to_dict() for row in s.scalars(query)]

    def list_events(
        self,
        s: Session,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        severity: str = "",
        source: str = "",
        text: str = "",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        query = select(SystemEventRow)
        if date_from:
            query = query.where(SystemEventRow.occurred_at >= date_from)
        if date_to:
            query = query.where(SystemEventRow.occurred_at < date_to)
        if severity:
            query = query.where(SystemEventRow.severity == severity)
        if source:
            query = query.where(SystemEventRow.source == source)
        if text.strip():
            t = text.strip()
            query = query.where(
                or_(
                    SystemEventRow.message.contains(t, autoescape=True),
                    SystemEventRow.event_type.contains(t, autoescape=True),
                    SystemEventRow.details.contains(t, autoescape=True),
                )
            )
        query = query.order_by(SystemEventRow.occurred_at.desc(), SystemEventRow.id.desc()).limit(limit)
        return [row.to_dict() for row in s.scalars(query)]
