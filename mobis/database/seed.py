"""Initial data for a freshly created database.

* Equipment master and routing links reproduce the physical setup described in
  the "Input Feeder Traceability" document (4 hoppers, 3 silos of 14 t,
  4 distribution tanks of 4 t).  Equipment barcodes are placeholders and must
  be replaced with the values printed on the physical labels.
* Bag types are DEMO data for testing the workflow; they are marked as such and
  can be deleted / deactivated by an administrator.
* An ``admin`` account is created with password ``admin`` and must change the
  password at first login.
"""

from __future__ import annotations

import logging

from mobis.core.models import EquipmentCategory, InventoryChangeType
from mobis.core.security import hash_password
from mobis.database.database import Database
from mobis.database.repositories import Repositories

log = logging.getLogger("mobis.database.seed")

H, S, D = EquipmentCategory.HOPPER, EquipmentCategory.SILO, EquipmentCategory.DISTRIBUTION_TANK

# (code, name, category, barcode placeholder, capacity kg, plc code placeholder, sort)
_EQUIPMENT = [
    ("HOP-01", "Hopper 1", H, "HOP-01", None, 1, 1),
    ("HOP-02", "Hopper 2", H, "HOP-02", None, 2, 2),
    ("HOP-03", "Hopper 3", H, "HOP-03", None, 3, 3),
    ("HOP-04", "Hopper 4", H, "HOP-04", None, 4, 4),
    ("SILO-01", "Silo 1", S, "SILO-01", 14000.0, 11, 1),
    ("SILO-02", "Silo 2", S, "SILO-02", 14000.0, 12, 2),
    ("SILO-03", "Silo 3", S, "SILO-03", 14000.0, 13, 3),
    ("DT-01", "Distribution Tank 1", D, "DT-01", 4000.0, 21, 1),
    ("DT-02", "Distribution Tank 2", D, "DT-02", 4000.0, 22, 2),
    ("DT-03", "Distribution Tank 3", D, "DT-03", 4000.0, 23, 3),
    ("DT-04", "Distribution Tank 4", D, "DT-04", 4000.0, 24, 4),
]

# Physical routing from the supporting document (configurable, not hard coded).
_LINKS = [
    ("HOP-01", "SILO-02"),
    ("HOP-02", "SILO-01"),
    ("HOP-03", "DT-02"),
    ("HOP-03", "DT-03"),
    ("HOP-03", "DT-04"),
    ("HOP-04", "DT-01"),
]

# DEMO bag types: (code, name, material, colour, colour code, prefix, kg, hopper, silo, dist)
_DEMO_BAG_TYPES = [
    ("ABS-BLK", "ABS - Black", "ABS", "Black", "BLK", "ABS-BLK-", 25.0, "HOP-01", "SILO-02", None),
    ("PP-NAT", "PP - Natural", "PP", "Natural", "NAT", "PP-NAT-", 25.0, "HOP-02", "SILO-01", None),
    ("PC-GRY", "PC - Grey", "PC", "Grey", "GRY", "PC-GRY-", 25.0, "HOP-03", None, "DT-02"),
    ("PA6-BLK", "PA6 - Black", "PA6", "Black", "BLK", "PA6-BLK-", 25.0, "HOP-03", None, "DT-03"),
    ("ABS-RED", "ABS - Red", "ABS", "Red", "RED", "ABS-RED-", 25.0, "HOP-04", None, "DT-01"),
]


def seed_initial_data(db: Database, include_demo_bag_types: bool = True) -> None:
    repos = Repositories()
    with db.session() as s:
        if repos.equipment.count(s):
            log.info("Seed skipped: equipment already present")
            return
        ids: dict[str, int] = {}
        for code, name, category, barcode, capacity, plc_code, sort in _EQUIPMENT:
            eq = repos.equipment.create(
                s,
                {
                    "equipment_code": code,
                    "name": name,
                    "category": category.value,
                    "barcode": barcode,
                    "description": "Initial configuration - replace barcode with physical label value",
                    "capacity_kg": capacity,
                    "plc_code": plc_code,
                    "ready_signal_address": "",
                    "ready_signal_ok_value": 1,
                    "is_active": True,
                    "sort_order": sort,
                },
            )
            ids[code] = eq.id
            if category in (S, D):
                repos.inventory.apply_change(
                    s, eq.id, InventoryChangeType.INITIAL, new_quantity_kg=0.0,
                    reason="Initial quantity (database created)", username="system",
                )
        for source, target in _LINKS:
            repos.equipment.add_link(s, ids[source], ids[target], "Physical setup (initial configuration)")

        if include_demo_bag_types:
            for code, name, material, colour, colour_code, prefix, kg, hopper, silo, dist in _DEMO_BAG_TYPES:
                repos.bag_types.create(
                    s,
                    {
                        "code": code,
                        "name": name,
                        "material_type": material,
                        "colour": colour,
                        "colour_code": colour_code,
                        "barcode_match_type": "PREFIX",
                        "barcode_match_value": prefix,
                        "weight_kg": kg,
                        "hopper_id": ids[hopper],
                        "silo_id": ids[silo] if silo else None,
                        "silo_scan_required": silo is not None,
                        "distribution_tank_id": ids[dist] if dist else None,
                        "distribution_scan_required": dist is not None,
                        "mold_id": None,
                        "mold_scan_required": False,
                        "description": "DEMO bag type - replace with real material definitions",
                        "is_active": True,
                    },
                )

        if repos.users.find_by_username(s, "admin") is None:
            repos.users.create(
                s,
                username="admin",
                full_name="Administrator",
                role="ADMIN",
                password_hash=hash_password("admin"),
                is_active=True,
                must_change_password=True,
                failed_attempts=0,
            )
        repos.audit.add_audit(
            s, username="system", category="DATABASE", action="SEED",
            details="Initial equipment, routing" + (", demo bag types" if include_demo_bag_types else "")
            + " and admin account created",
        )
    log.info("Initial data seeded (demo bag types: %s)", include_demo_bag_types)
