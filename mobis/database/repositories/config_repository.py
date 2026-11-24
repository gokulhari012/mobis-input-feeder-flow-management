from __future__ import annotations

from typing import Type

from sqlalchemy import select
from sqlalchemy.orm import Session

from mobis.utils.timeutil import utc_now


class ConfigRepository:
    """Key/value storage for configuration sections (values are JSON text)."""

    def load(self, s: Session, model: Type, prefix: str = "") -> dict[str, str]:
        rows = s.scalars(select(model)).all()
        return {row.key[len(prefix):]: row.value for row in rows if row.key.startswith(prefix)}

    def save(self, s: Session, model: Type, prefix: str, values: dict[str, str], username: str = "") -> None:
        now = utc_now()
        for key, value in values.items():
            full_key = f"{prefix}{key}"
            row = s.get(model, full_key)
            if row is None:
                s.add(model(key=full_key, value=value, updated_at=now, updated_by=username))
            else:
                row.value = value
                row.updated_at = now
                row.updated_by = username
        s.flush()
