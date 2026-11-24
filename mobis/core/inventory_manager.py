"""Material quantity monitoring.

Updated tank quantity = existing tank quantity + bag weight (kg).

Quantities are only increased by *confirmed* loadings (never by a scan), inside
the same database transaction that completes the loading transaction.  Manual
corrections (physical stock count) are recorded in the inventory history and
the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from mobis.core.models import Equipment, InventoryChangeType
from mobis.database.database import Database
from mobis.database.repositories import Repositories


def compute_updated_quantity(existing_kg: float, bag_weight_kg: float) -> float:
    """The quantity formula from the specification (rounded to grams)."""
    if bag_weight_kg < 0:
        raise ValueError("Bag weight cannot be negative")
    return round(float(existing_kg) + float(bag_weight_kg), 3)


@dataclass(frozen=True)
class TankLevel:
    equipment: Equipment
    quantity_kg: float
    last_change_at: Optional[datetime]
    last_correction_at: Optional[datetime]

    @property
    def capacity_kg(self) -> Optional[float]:
        return self.equipment.capacity_kg

    @property
    def fill_ratio(self) -> Optional[float]:
        if not self.capacity_kg:
            return None
        return self.quantity_kg / self.capacity_kg

    @property
    def over_capacity(self) -> bool:
        return bool(self.capacity_kg) and self.quantity_kg > self.capacity_kg


class InventoryManager:
    def __init__(self, db: Database, repos: Repositories, audit=None):
        self._db = db
        self._repos = repos
        self._audit = audit

    def levels(self, include_inactive: bool = False) -> list[TankLevel]:
        with self._db.read_session() as s:
            rows = self._repos.inventory.list_levels(s, include_inactive)
        return [
            TankLevel(r["equipment"], r["quantity_kg"], r["last_change_at"], r["last_correction_at"]) for r in rows
        ]

    def quantity(self, equipment_id: int) -> float:
        with self._db.read_session() as s:
            return self._repos.inventory.quantity(s, equipment_id)

    def apply_loading(
        self, session: Session, equipment_id: int, bag_weight_kg: float, transaction_id: int, username: str
    ) -> tuple[float, float]:
        """Increase the tank quantity for a confirmed loading (caller's unit of work)."""
        existing = self._repos.inventory.quantity(session, equipment_id)
        updated = compute_updated_quantity(existing, bag_weight_kg)
        return self._repos.inventory.apply_change(
            session, equipment_id, InventoryChangeType.LOADING,
            new_quantity_kg=updated, reason="Material loading", username=username,
            transaction_id=transaction_id,
        )

    def correct(self, equipment_id: int, new_quantity_kg: float, reason: str, username: str) -> tuple[float, float]:
        """Administrator correction of a physical inventory difference."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("A reason is required for an inventory correction.")
        if new_quantity_kg < 0:
            raise ValueError("Quantity cannot be negative.")
        with self._db.session() as s:
            equipment = self._repos.equipment.get(s, equipment_id)
            if equipment is None:
                raise ValueError("Equipment not found.")
            before, after = self._repos.inventory.apply_change(
                s, equipment_id, InventoryChangeType.CORRECTION,
                new_quantity_kg=new_quantity_kg, reason=reason, username=username,
            )
            if self._audit is not None:
                self._audit.log(
                    "INVENTORY", "CORRECTION",
                    f"{equipment.name}: {before:.3f} kg -> {after:.3f} kg ({after - before:+.3f} kg). Reason: {reason}",
                    entity="tank_inventory", entity_id=equipment_id,
                    old={"quantity_kg": before}, new={"quantity_kg": after}, username=username, session=s,
                )
        return before, after

    def history(self, equipment_id: Optional[int] = None, limit: int = 500) -> list[dict[str, Any]]:
        with self._db.read_session() as s:
            return self._repos.inventory.history(s, equipment_id, limit)
