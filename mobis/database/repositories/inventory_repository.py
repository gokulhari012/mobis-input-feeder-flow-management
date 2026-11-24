from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from mobis.core.models import STORAGE_CATEGORIES, InventoryChangeType
from mobis.database.models import EquipmentRow, InventoryHistoryRow, TankInventoryRow, TransactionRow
from mobis.utils.timeutil import utc_now


def _round(value: float) -> float:
    return round(float(value), 3)


class InventoryRepository:
    def get(self, s: Session, equipment_id: int) -> Optional[TankInventoryRow]:
        return s.get(TankInventoryRow, equipment_id)

    def get_or_create(self, s: Session, equipment_id: int) -> TankInventoryRow:
        row = s.get(TankInventoryRow, equipment_id)
        if row is None:
            now = utc_now()
            row = TankInventoryRow(
                equipment_id=equipment_id, quantity_kg=0.0, last_change_at=now, updated_at=now
            )
            s.add(row)
            s.flush()
        return row

    def quantity(self, s: Session, equipment_id: int) -> float:
        row = s.get(TankInventoryRow, equipment_id)
        return float(row.quantity_kg) if row else 0.0

    def apply_change(
        self,
        s: Session,
        equipment_id: int,
        change_type: InventoryChangeType,
        *,
        delta_kg: Optional[float] = None,
        new_quantity_kg: Optional[float] = None,
        reason: str = "",
        username: str = "",
        transaction_id: Optional[int] = None,
    ) -> tuple[float, float]:
        """Apply a quantity change and record it in the history (same DB transaction)."""
        if (delta_kg is None) == (new_quantity_kg is None):
            raise ValueError("Specify exactly one of delta_kg or new_quantity_kg")
        row = self.get_or_create(s, equipment_id)
        before = _round(row.quantity_kg)
        after = _round(before + delta_kg) if delta_kg is not None else _round(new_quantity_kg)
        now = utc_now()
        row.quantity_kg = after
        row.last_change_at = now
        row.updated_at = now
        if transaction_id is not None:
            row.last_transaction_id = transaction_id
        if change_type in (InventoryChangeType.CORRECTION, InventoryChangeType.INITIAL):
            row.last_correction_at = now
        s.add(
            InventoryHistoryRow(
                equipment_id=equipment_id,
                changed_at=now,
                change_type=change_type.value,
                quantity_before_kg=before,
                change_kg=_round(after - before),
                quantity_after_kg=after,
                transaction_id=transaction_id,
                reason=reason,
                username=username,
            )
        )
        s.flush()
        return before, after

    def list_levels(self, s: Session, include_inactive: bool = False) -> list[dict[str, Any]]:
        query = (
            select(EquipmentRow, TankInventoryRow)
            .outerjoin(TankInventoryRow, TankInventoryRow.equipment_id == EquipmentRow.id)
            .where(EquipmentRow.category.in_([c.value for c in STORAGE_CATEGORIES]))
            .order_by(EquipmentRow.category.desc(), EquipmentRow.sort_order, EquipmentRow.name)
        )
        if not include_inactive:
            query = query.where(EquipmentRow.is_active.is_(True))
        result = []
        for eq, inv in s.execute(query):
            result.append(
                {
                    "equipment": eq.to_domain(),
                    "quantity_kg": float(inv.quantity_kg) if inv else 0.0,
                    "last_change_at": inv.last_change_at if inv else None,
                    "last_correction_at": inv.last_correction_at if inv else None,
                    "last_transaction_id": inv.last_transaction_id if inv else None,
                }
            )
        return result

    def history(self, s: Session, equipment_id: Optional[int] = None, limit: int = 500) -> list[dict[str, Any]]:
        query = (
            select(InventoryHistoryRow, EquipmentRow.name, TransactionRow.transaction_no)
            .join(EquipmentRow, EquipmentRow.id == InventoryHistoryRow.equipment_id)
            .outerjoin(TransactionRow, TransactionRow.id == InventoryHistoryRow.transaction_id)
            .order_by(InventoryHistoryRow.changed_at.desc(), InventoryHistoryRow.id.desc())
            .limit(limit)
        )
        if equipment_id is not None:
            query = query.where(InventoryHistoryRow.equipment_id == equipment_id)
        return [
            dict(row.to_dict(), equipment_name=name, transaction_no=no or "")
            for row, name, no in s.execute(query)
        ]
