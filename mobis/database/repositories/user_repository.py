from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mobis.database.models import UserRow
from mobis.utils.timeutil import utc_now


class UserRepository:
    def list(self, s: Session) -> list[UserRow]:
        return list(s.scalars(select(UserRow).order_by(UserRow.username)))

    def get(self, s: Session, user_id: int) -> Optional[UserRow]:
        return s.get(UserRow, user_id)

    def find_by_username(self, s: Session, username: str) -> Optional[UserRow]:
        return s.scalars(select(UserRow).where(UserRow.username == username.strip())).first()

    def create(self, s: Session, **fields: Any) -> UserRow:
        now = utc_now()
        row = UserRow(created_at=now, updated_at=now, **fields)
        s.add(row)
        s.flush()
        return row

    def update(self, s: Session, user_id: int, **fields: Any) -> UserRow:
        row = s.get(UserRow, user_id)
        if row is None:
            raise KeyError(f"User {user_id} not found")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()
        s.flush()
        return row

    def count_active_admins(self, s: Session, exclude_id: Optional[int] = None) -> int:
        query = select(func.count()).select_from(UserRow).where(UserRow.role == "ADMIN", UserRow.is_active.is_(True))
        if exclude_id is not None:
            query = query.where(UserRow.id != exclude_id)
        return int(s.scalar(query) or 0)
