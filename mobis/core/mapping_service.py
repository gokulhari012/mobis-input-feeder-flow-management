"""Equipment master data, routing links and bag type mappings (with audit)."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable, Optional

from mobis.core.models import (
    STORAGE_CATEGORIES,
    BagType,
    BarcodeMatchType,
    Equipment,
    EquipmentCategory,
    EquipmentLink,
    InventoryChangeType,
)
from mobis.core.validation_engine import ConfigSnapshot, ValidationEngine
from mobis.database.database import Database
from mobis.database.repositories import Repositories

log = logging.getLogger("mobis.mapping")

#: Validates a PLC device address; returns an error message or "".
AddressValidator = Callable[[str], str]


class MappingError(ValueError):
    pass


class MappingService:
    def __init__(self, db: Database, repos: Repositories, config, audit, inventory=None):
        self._db = db
        self._repos = repos
        self._config = config
        self._audit = audit
        self._inventory = inventory

    # ------------------------------------------------------------------ snapshot
    def load_snapshot(self) -> ConfigSnapshot:
        with self._db.read_session() as s:
            equipment = {e.id: e for e in self._repos.equipment.list(s)}
            links = frozenset(
                (link.source_id, link.target_id) for link in self._repos.equipment.list_links(s, active_only=True)
            )
            bag_types = {b.id: b for b in self._repos.bag_types.list(s)}
        return ConfigSnapshot(
            equipment=equipment,
            links=links,
            bag_types=bag_types,
            barcode_format=self._config.barcode(),
            mold_verification_enabled=self._config.app().mold_verification_enabled,
        )

    # ------------------------------------------------------------------ equipment
    def list_equipment(self, category: Optional[EquipmentCategory] = None, active_only: bool = False) -> list[Equipment]:
        with self._db.read_session() as s:
            return self._repos.equipment.list(s, category, active_only)

    def get_equipment(self, equipment_id: int) -> Optional[Equipment]:
        with self._db.read_session() as s:
            return self._repos.equipment.get(s, equipment_id)

    def barcode_used_in_other_categories(self, barcode: str, category: str, exclude_id: Optional[int]) -> list[Equipment]:
        with self._db.read_session() as s:
            return [
                e for e in self._repos.equipment.find_by_barcode(s, barcode, exclude_id=exclude_id)
                if e.category.value != category
            ]

    def save_equipment(
        self,
        data: dict[str, Any],
        equipment_id: Optional[int] = None,
        username: str = "",
        initial_quantity_kg: Optional[float] = None,
        address_validator: Optional[AddressValidator] = None,
    ) -> Equipment:
        data = dict(data)
        for key in ("equipment_code", "name", "barcode", "description", "ready_signal_address"):
            data[key] = str(data.get(key) or "").strip()
        errors = []
        if not data["equipment_code"]:
            errors.append("Equipment ID is required.")
        if not data["name"]:
            errors.append("Equipment name is required.")
        if not data["barcode"]:
            errors.append("Barcode value is required.")
        try:
            category = EquipmentCategory(data.get("category"))
        except ValueError:
            errors.append("A valid equipment category is required.")
            category = None
        capacity = data.get("capacity_kg")
        if capacity is not None and capacity < 0:
            errors.append("Capacity cannot be negative.")
        if data["ready_signal_address"] and address_validator:
            problem = address_validator(data["ready_signal_address"])
            if problem:
                errors.append(f"Limit switch / ready signal: {problem}")
        if errors:
            raise MappingError("\n".join(errors))

        with self._db.session() as s:
            repo = self._repos.equipment
            if repo.find_by_code(s, data["equipment_code"], exclude_id=equipment_id):
                raise MappingError(f"Equipment ID '{data['equipment_code']}' is already used.")
            if repo.find_by_barcode(s, data["barcode"], category.value, exclude_id=equipment_id):
                raise MappingError(f"Barcode '{data['barcode']}' is already used by another {category.label.lower()}.")
            if equipment_id is None:
                eq = repo.create(s, data)
                self._audit.log("EQUIPMENT", "CREATE", f"Equipment {eq.code} ({eq.name}) created",
                                entity="equipment", entity_id=eq.id, new=data, username=username, session=s)
                if category in STORAGE_CATEGORIES:
                    qty = max(0.0, float(initial_quantity_kg or 0.0))
                    self._repos.inventory.apply_change(
                        s, eq.id, InventoryChangeType.INITIAL, new_quantity_kg=qty,
                        reason="Initial quantity (equipment created)", username=username,
                    )
                return eq
            current = repo.get(s, equipment_id)
            if current is None:
                raise MappingError("Equipment not found.")
            if current.category != category:
                users = repo.referencing_bag_types(s, equipment_id, active_only=False)
                if users:
                    raise MappingError(
                        "The category cannot be changed while bag types reference this equipment: " + ", ".join(users)
                    )
            old, new = repo.update(s, equipment_id, data)
            self._audit.log("EQUIPMENT", "UPDATE", f"Equipment {new['equipment_code']} updated",
                            entity="equipment", entity_id=equipment_id, old=old, new=new, username=username, session=s)
            return repo.get(s, equipment_id)

    def set_equipment_active(self, equipment_id: int, active: bool, username: str = "") -> None:
        with self._db.session() as s:
            eq = self._repos.equipment.get(s, equipment_id)
            if eq is None:
                raise MappingError("Equipment not found.")
            self._repos.equipment.set_active(s, equipment_id, active)
            self._audit.log("EQUIPMENT", "ACTIVATE" if active else "DEACTIVATE",
                            f"Equipment {eq.code} ({eq.name}) {'activated' if active else 'deactivated'}",
                            entity="equipment", entity_id=equipment_id, username=username, session=s)

    def bag_types_using(self, equipment_id: int) -> list[str]:
        with self._db.read_session() as s:
            return self._repos.equipment.referencing_bag_types(s, equipment_id)

    # ------------------------------------------------------------------ routing
    def list_links(self) -> list[EquipmentLink]:
        with self._db.read_session() as s:
            return self._repos.equipment.list_links(s)

    def add_link(self, source_id: int, target_id: int, description: str = "", username: str = "") -> EquipmentLink:
        if source_id == target_id:
            raise MappingError("Source and target must be different equipment.")
        with self._db.session() as s:
            src = self._repos.equipment.get(s, source_id)
            tgt = self._repos.equipment.get(s, target_id)
            if src is None or tgt is None:
                raise MappingError("Equipment not found.")
            allowed = {
                EquipmentCategory.HOPPER: {EquipmentCategory.SILO, EquipmentCategory.DISTRIBUTION_TANK,
                                           EquipmentCategory.MOLD},
                EquipmentCategory.SILO: {EquipmentCategory.DISTRIBUTION_TANK, EquipmentCategory.MOLD},
                EquipmentCategory.DISTRIBUTION_TANK: {EquipmentCategory.MOLD},
            }
            if tgt.category not in allowed.get(src.category, set()):
                raise MappingError(f"A {src.category.label.lower()} cannot feed a {tgt.category.label.lower()}.")
            if self._repos.equipment.find_link(s, source_id, target_id):
                raise MappingError(f"{src.name} → {tgt.name} already exists.")
            link = self._repos.equipment.add_link(s, source_id, target_id, description.strip())
            self._audit.log("ROUTING", "CREATE", f"Routing link {src.name} → {tgt.name} added",
                            entity="equipment_mapping", entity_id=link.id, username=username, session=s)
            return link

    def set_link_active(self, link_id: int, active: bool, username: str = "") -> None:
        with self._db.session() as s:
            self._repos.equipment.set_link_active(s, link_id, active)
            self._audit.log("ROUTING", "ACTIVATE" if active else "DEACTIVATE",
                            f"Routing link {link_id} {'activated' if active else 'deactivated'}",
                            entity="equipment_mapping", entity_id=link_id, username=username, session=s)

    def delete_link(self, link_id: int, username: str = "") -> None:
        with self._db.session() as s:
            self._repos.equipment.delete_link(s, link_id)
            self._audit.log("ROUTING", "DELETE", f"Routing link {link_id} deleted",
                            entity="equipment_mapping", entity_id=link_id, username=username, session=s)

    # ------------------------------------------------------------------ bag types
    def list_bag_types(self, active_only: bool = False) -> list[BagType]:
        with self._db.read_session() as s:
            return self._repos.bag_types.list(s, active_only)

    def validate_bag_type(self, candidate: BagType) -> list[str]:
        """Full validation of a (new or edited) bag type against the current configuration."""
        errors = []
        if not candidate.code.strip():
            errors.append("Bag type code is required.")
        if not candidate.name.strip():
            errors.append("Bag type name is required.")
        if not candidate.material_type.strip():
            errors.append("Material type is required.")
        if not candidate.colour.strip():
            errors.append("Colour is required.")
        with self._db.read_session() as s:
            if candidate.code.strip() and self._repos.bag_types.find_by_code(
                s, candidate.code, exclude_id=candidate.id or None
            ):
                errors.append(f"Bag type code '{candidate.code}' is already used.")
        snapshot = self.load_snapshot()
        errors.extend(ValidationEngine(snapshot).check_bag_type(candidate))
        return errors

    def save_bag_type(self, candidate: BagType, username: str = "") -> BagType:
        candidate = replace(
            candidate,
            code=candidate.code.strip(),
            name=candidate.name.strip(),
            material_type=candidate.material_type.strip(),
            colour=candidate.colour.strip(),
            colour_code=candidate.colour_code.strip(),
            match_value=candidate.match_value.strip(),
        )
        errors = self.validate_bag_type(candidate)
        if errors:
            raise MappingError("\n".join(errors))
        data = {
            "code": candidate.code,
            "name": candidate.name,
            "material_type": candidate.material_type,
            "colour": candidate.colour,
            "colour_code": candidate.colour_code,
            "barcode_match_type": BarcodeMatchType(candidate.match_type).value,
            "barcode_match_value": candidate.match_value,
            "weight_kg": float(candidate.weight_kg),
            "hopper_id": candidate.hopper_id,
            "silo_id": candidate.silo_id,
            "silo_scan_required": candidate.silo_scan_required,
            "distribution_tank_id": candidate.distribution_tank_id,
            "distribution_scan_required": candidate.distribution_scan_required,
            "mold_id": candidate.mold_id,
            "mold_scan_required": candidate.mold_scan_required,
            "description": candidate.description,
            "is_active": candidate.is_active,
        }
        with self._db.session() as s:
            if not candidate.id:
                bag_type = self._repos.bag_types.create(s, data)
                self._audit.log("BAG_TYPE", "CREATE", f"Bag type {bag_type.code} ({bag_type.name}) created",
                                entity="bag_type", entity_id=bag_type.id, new=data, username=username, session=s)
                return bag_type
            old, new = self._repos.bag_types.update(s, candidate.id, data)
            self._audit.log("BAG_TYPE", "UPDATE", f"Bag type {candidate.code} updated",
                            entity="bag_type", entity_id=candidate.id, old=old, new=new, username=username, session=s)
            return self._repos.bag_types.get(s, candidate.id)

    def set_bag_type_active(self, bag_type_id: int, active: bool, username: str = "") -> None:
        with self._db.session() as s:
            bag_type = self._repos.bag_types.get(s, bag_type_id)
            if bag_type is None:
                raise MappingError("Bag type not found.")
            self._repos.bag_types.set_active(s, bag_type_id, active)
            self._audit.log("BAG_TYPE", "ACTIVATE" if active else "DEACTIVATE",
                            f"Bag type {bag_type.code} {'activated' if active else 'deactivated'}",
                            entity="bag_type", entity_id=bag_type_id, username=username, session=s)

    def bag_type_usage(self, bag_type_id: int) -> int:
        with self._db.read_session() as s:
            return self._repos.bag_types.usage_count(s, bag_type_id)

    def delete_bag_type(self, bag_type_id: int, username: str = "") -> None:
        with self._db.session() as s:
            bag_type = self._repos.bag_types.get(s, bag_type_id)
            if bag_type is None:
                return
            try:
                self._repos.bag_types.delete(s, bag_type_id)
            except ValueError as exc:
                raise MappingError(str(exc)) from exc
            self._audit.log("BAG_TYPE", "DELETE", f"Bag type {bag_type.code} ({bag_type.name}) deleted",
                            entity="bag_type", entity_id=bag_type_id, username=username, session=s)
