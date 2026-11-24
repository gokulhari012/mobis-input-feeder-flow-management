from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mobis.core.models import BagType
from mobis.database.models import BagTypeRow, TransactionRow
from mobis.utils.timeutil import utc_now

BAG_TYPE_FIELDS = (
    "code", "name", "material_type", "colour", "colour_code", "barcode_match_type", "barcode_match_value",
    "weight_kg", "hopper_id", "silo_id", "silo_scan_required", "distribution_tank_id",
    "distribution_scan_required", "mold_id", "mold_scan_required", "description", "is_active",
)


class BagTypeRepository:
    def list(self, s: Session, active_only: bool = False) -> list[BagType]:
        query = select(BagTypeRow).order_by(BagTypeRow.name)
        if active_only:
            query = query.where(BagTypeRow.is_active.is_(True))
        return [row.to_domain() for row in s.scalars(query)]

    def get(self, s: Session, bag_type_id: int) -> Optional[BagType]:
        row = s.get(BagTypeRow, bag_type_id)
        return row.to_domain() if row else None

    def find_by_code(self, s: Session, code: str, exclude_id: Optional[int] = None) -> Optional[BagType]:
        query = select(BagTypeRow).where(BagTypeRow.code == code.strip())
        if exclude_id is not None:
            query = query.where(BagTypeRow.id != exclude_id)
        row = s.scalars(query).first()
        return row.to_domain() if row else None

    def create(self, s: Session, data: dict[str, Any]) -> BagType:
        now = utc_now()
        row = BagTypeRow(**{k: data[k] for k in BAG_TYPE_FIELDS if k in data}, created_at=now, updated_at=now)
        s.add(row)
        s.flush()
        return row.to_domain()

    def update(self, s: Session, bag_type_id: int, data: dict[str, Any]) -> tuple[dict, dict]:
        row = s.get(BagTypeRow, bag_type_id)
        if row is None:
            raise KeyError(f"Bag type {bag_type_id} not found")
        old = {k: getattr(row, k) for k in BAG_TYPE_FIELDS}
        for key in BAG_TYPE_FIELDS:
            if key in data:
                setattr(row, key, data[key])
        row.updated_at = utc_now()
        s.flush()
        return old, {k: getattr(row, k) for k in BAG_TYPE_FIELDS}

    def set_active(self, s: Session, bag_type_id: int, active: bool) -> None:
        row = s.get(BagTypeRow, bag_type_id)
        if row is None:
            raise KeyError(f"Bag type {bag_type_id} not found")
        row.is_active = active
        row.updated_at = utc_now()

    def usage_count(self, s: Session, bag_type_id: int) -> int:
        return int(
            s.scalar(select(func.count()).select_from(TransactionRow).where(TransactionRow.bag_type_id == bag_type_id))
            or 0
        )

    def delete(self, s: Session, bag_type_id: int) -> None:
        if self.usage_count(s, bag_type_id):
            raise ValueError("Bag type is referenced by transactions and cannot be deleted; deactivate it instead.")
        row = s.get(BagTypeRow, bag_type_id)
        if row is not None:
            s.delete(row)
