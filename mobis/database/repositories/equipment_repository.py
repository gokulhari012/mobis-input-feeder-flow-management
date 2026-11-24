from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from mobis.core.models import Equipment, EquipmentCategory, EquipmentLink
from mobis.database.models import BagTypeRow, EquipmentMappingRow, EquipmentRow
from mobis.utils.timeutil import utc_now

EQUIPMENT_FIELDS = (
    "equipment_code", "name", "category", "barcode", "description", "capacity_kg", "plc_code",
    "ready_signal_address", "ready_signal_ok_value", "is_active", "sort_order",
)

_CATEGORY_ORDER = case(
    {"HOPPER": 0, "SILO": 1, "DISTRIBUTION_TANK": 2, "MOLD": 3},
    value=EquipmentRow.category,
    else_=9,
)


class EquipmentRepository:
    # ------------------------------------------------------------------ equipment
    def list(
        self, s: Session, category: Optional[EquipmentCategory | str] = None, active_only: bool = False
    ) -> list[Equipment]:
        query = select(EquipmentRow)
        if category:
            query = query.where(EquipmentRow.category == EquipmentCategory(category).value)
        if active_only:
            query = query.where(EquipmentRow.is_active.is_(True))
        query = query.order_by(_CATEGORY_ORDER, EquipmentRow.sort_order, EquipmentRow.name)
        return [row.to_domain() for row in s.scalars(query)]

    def get(self, s: Session, equipment_id: int) -> Optional[Equipment]:
        row = s.get(EquipmentRow, equipment_id)
        return row.to_domain() if row else None

    def find_by_code(self, s: Session, code: str, exclude_id: Optional[int] = None) -> Optional[Equipment]:
        query = select(EquipmentRow).where(EquipmentRow.equipment_code == code.strip())
        if exclude_id is not None:
            query = query.where(EquipmentRow.id != exclude_id)
        row = s.scalars(query).first()
        return row.to_domain() if row else None

    def find_by_barcode(
        self, s: Session, barcode: str, category: Optional[str] = None, exclude_id: Optional[int] = None
    ) -> list[Equipment]:
        query = select(EquipmentRow).where(EquipmentRow.barcode == barcode.strip())
        if category:
            query = query.where(EquipmentRow.category == EquipmentCategory(category).value)
        if exclude_id is not None:
            query = query.where(EquipmentRow.id != exclude_id)
        return [row.to_domain() for row in s.scalars(query)]

    def create(self, s: Session, data: dict[str, Any]) -> Equipment:
        now = utc_now()
        row = EquipmentRow(**{k: data[k] for k in EQUIPMENT_FIELDS if k in data}, created_at=now, updated_at=now)
        s.add(row)
        s.flush()
        return row.to_domain()

    def update(self, s: Session, equipment_id: int, data: dict[str, Any]) -> tuple[dict, dict]:
        row = s.get(EquipmentRow, equipment_id)
        if row is None:
            raise KeyError(f"Equipment {equipment_id} not found")
        old = {k: getattr(row, k) for k in EQUIPMENT_FIELDS}
        for key in EQUIPMENT_FIELDS:
            if key in data:
                setattr(row, key, data[key])
        row.updated_at = utc_now()
        s.flush()
        return old, {k: getattr(row, k) for k in EQUIPMENT_FIELDS}

    def set_active(self, s: Session, equipment_id: int, active: bool) -> None:
        row = s.get(EquipmentRow, equipment_id)
        if row is None:
            raise KeyError(f"Equipment {equipment_id} not found")
        row.is_active = active
        row.updated_at = utc_now()

    def referencing_bag_types(self, s: Session, equipment_id: int, active_only: bool = True) -> list[str]:
        query = select(BagTypeRow.name).where(
            or_(
                BagTypeRow.hopper_id == equipment_id,
                BagTypeRow.silo_id == equipment_id,
                BagTypeRow.distribution_tank_id == equipment_id,
                BagTypeRow.mold_id == equipment_id,
            )
        )
        if active_only:
            query = query.where(BagTypeRow.is_active.is_(True))
        return list(s.scalars(query))

    def count(self, s: Session) -> int:
        return int(s.scalar(select(func.count()).select_from(EquipmentRow)) or 0)

    # ------------------------------------------------------------------ routing links
    def list_links(self, s: Session, active_only: bool = False) -> list[EquipmentLink]:
        query = select(EquipmentMappingRow).order_by(
            EquipmentMappingRow.source_equipment_id, EquipmentMappingRow.target_equipment_id
        )
        if active_only:
            query = query.where(EquipmentMappingRow.is_active.is_(True))
        return [row.to_domain() for row in s.scalars(query)]

    def find_link(self, s: Session, source_id: int, target_id: int) -> Optional[EquipmentLink]:
        row = s.scalars(
            select(EquipmentMappingRow).where(
                EquipmentMappingRow.source_equipment_id == source_id,
                EquipmentMappingRow.target_equipment_id == target_id,
            )
        ).first()
        return row.to_domain() if row else None

    def add_link(self, s: Session, source_id: int, target_id: int, description: str = "") -> EquipmentLink:
        now = utc_now()
        row = EquipmentMappingRow(
            source_equipment_id=source_id,
            target_equipment_id=target_id,
            is_active=True,
            description=description,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        s.flush()
        return row.to_domain()

    def set_link_active(self, s: Session, link_id: int, active: bool) -> None:
        row = s.get(EquipmentMappingRow, link_id)
        if row is None:
            raise KeyError(f"Link {link_id} not found")
        row.is_active = active
        row.updated_at = utc_now()

    def delete_link(self, s: Session, link_id: int) -> None:
        row = s.get(EquipmentMappingRow, link_id)
        if row is not None:
            s.delete(row)
