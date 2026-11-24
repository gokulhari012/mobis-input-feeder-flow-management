"""Material quantity monitoring."""

from __future__ import annotations

import pytest

from mobis.core.inventory_manager import InventoryManager, compute_updated_quantity
from mobis.database.repositories import Repositories
from mobis.services.audit_service import AuditService


def _silo_id(db, repos, code="SILO-02"):
    with db.read_session() as s:
        return repos.equipment.find_by_code(s, code).id


def test_quantity_formula():
    assert compute_updated_quantity(3200.0, 25.0) == 3225.0
    assert compute_updated_quantity(0.1, 0.2) == 0.3
    with pytest.raises(ValueError):
        compute_updated_quantity(10, -1)


def test_loading_increments_quantity_and_records_history(seeded_db):
    repos = Repositories()
    manager = InventoryManager(seeded_db, repos, AuditService(seeded_db, repos))
    silo = _silo_id(seeded_db, repos)
    with seeded_db.session() as s:
        before, after = manager.apply_loading(s, silo, 25.0, transaction_id=None, username="op1")
    assert (before, after) == (0.0, 25.0)
    with seeded_db.session() as s:
        manager.apply_loading(s, silo, 25.0, transaction_id=None, username="op1")
    assert manager.quantity(silo) == 50.0
    history = manager.history(silo)
    assert [h["change_type"] for h in history[:2]] == ["LOADING", "LOADING"]
    assert history[0]["quantity_before_kg"] == 25.0 and history[0]["quantity_after_kg"] == 50.0


def test_loading_is_rolled_back_with_its_unit_of_work(seeded_db):
    repos = Repositories()
    manager = InventoryManager(seeded_db, repos)
    silo = _silo_id(seeded_db, repos)
    with pytest.raises(RuntimeError):
        with seeded_db.session() as s:
            manager.apply_loading(s, silo, 25.0, transaction_id=None, username="op1")
            raise RuntimeError("transaction record could not be written")
    assert manager.quantity(silo) == 0.0


def test_correction_requires_reason_and_is_audited(seeded_db):
    repos = Repositories()
    manager = InventoryManager(seeded_db, repos, AuditService(seeded_db, repos))
    silo = _silo_id(seeded_db, repos)
    with pytest.raises(ValueError):
        manager.correct(silo, 100.0, "  ", "admin")
    with pytest.raises(ValueError):
        manager.correct(silo, -5.0, "stock count", "admin")
    before, after = manager.correct(silo, 1234.5, "Physical stock count", "admin")
    assert (before, after) == (0.0, 1234.5)
    assert manager.history(silo)[0]["change_type"] == "CORRECTION"
    with seeded_db.read_session() as s:
        audit = repos.audit.list_audit(s, category="INVENTORY")
    assert audit and "Physical stock count" in audit[0]["details"]


def test_levels_list_storage_equipment_only(seeded_db):
    repos = Repositories()
    levels = InventoryManager(seeded_db, repos).levels()
    categories = {lvl.equipment.category.value for lvl in levels}
    assert categories == {"SILO", "DISTRIBUTION_TANK"}
    assert len(levels) == 7
    silo = next(lvl for lvl in levels if lvl.equipment.code == "SILO-01")
    assert silo.capacity_kg == 14000 and silo.fill_ratio == 0.0
