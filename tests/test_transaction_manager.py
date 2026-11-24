"""Transaction manager: full operator workflow against a real database and a fake PLC."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from mobis.core.inventory_manager import InventoryManager
from mobis.core.mapping_service import MappingService
from mobis.core.models import CheckItem, CheckResult, NgReason
from mobis.core.process_state_machine import ProcessState as S
from mobis.core.transaction_manager import TransactionManager
from mobis.database.models import BarcodeScanRow, MesQueueRow, TransactionRow
from mobis.database.repositories import Repositories
from mobis.integrations.mes_payload import enqueue_transaction
from mobis.services.audit_service import AuditService, EventService
from mobis.services.configuration_service import ConfigurationService

from .conftest import FakeAuth, FakePlc


class Env:
    def __init__(self, db, tmp_path):
        self.db = db
        self.repos = Repositories()
        self.audit = AuditService(db, self.repos)
        self.config = ConfigurationService(db, self.repos, self.audit)
        self.events = EventService(db, self.repos)
        self.inventory = InventoryManager(db, self.repos, self.audit)
        self.mapping = MappingService(db, self.repos, self.config, self.audit, self.inventory)
        self.plc = FakePlc()
        self.tm = TransactionManager(
            db, self.repos, self.config, self.mapping, self.inventory, self.plc, self.events, self.audit,
            FakeAuth(), tmp_path / "recovery",
            lambda s, tx: enqueue_transaction(s, self.repos, tx, self.config.mes()),
        )

    def bag_type_id(self, code):
        return next(b.id for b in self.mapping.list_bag_types() if b.code == code)

    def equipment_id(self, code):
        return next(e.id for e in self.mapping.list_equipment() if e.code == code)

    def quantity(self, code):
        return self.inventory.quantity(self.equipment_id(code))

    def transactions(self):
        with self.db.read_session() as s:
            return list(s.scalars(select(TransactionRow).order_by(TransactionRow.id)))

    def scans(self):
        with self.db.read_session() as s:
            return list(s.scalars(select(BarcodeScanRow).order_by(BarcodeScanRow.id)))

    def queue(self):
        with self.db.read_session() as s:
            return list(s.scalars(select(MesQueueRow)))


@pytest.fixture
def env(qapp, seeded_db, tmp_path):
    return Env(seeded_db, tmp_path)


def _run_to_authorized(env, bag="ABS-BLK-000001"):
    ok, msg = env.tm.select_bag_type(env.bag_type_id("ABS-BLK"))
    assert ok, msg
    env.tm.process_scan(bag, "SCANNER_HID")
    env.tm.process_scan("HOP-01", "SCANNER_HID")
    env.tm.process_scan("SILO-02", "SCANNER_HID")


def test_complete_workflow_updates_inventory_and_queues_mes(env):
    _run_to_authorized(env)
    assert env.tm.state == S.LOADING_AUTHORIZED
    assert env.plc.authorize_calls == [(1, 12)]  # PLC codes of Hopper 1 / Silo 2 from the seed
    assert env.quantity("SILO-02") == 0.0      # scanning alone never changes the quantity
    ok, _ = env.tm.confirm_loading_complete()
    assert ok and env.tm.state == S.LOADING_COMPLETED
    assert env.quantity("SILO-02") == 25.0
    assert env.plc.cleared == 1

    tx = env.transactions()[-1]
    assert tx.status == "COMPLETED" and tx.validation_status == "OK"
    assert (tx.quantity_before_kg, tx.quantity_after_kg) == (0.0, 25.0)
    assert tx.bag_barcode == "ABS-BLK-000001" and tx.operator_username == "op1"
    assert tx.distribution_result == "NA" and tx.silo_result == "OK"
    assert [s.result for s in env.scans()] == ["OK", "OK", "OK"]
    queue = env.queue()
    assert len(queue) == 1 and queue[0].status == "PENDING" and queue[0].idempotency_key == tx.transaction_uuid


def test_wrong_hopper_triggers_alarm_and_blocks_until_reset(env):
    env.tm.select_bag_type(env.bag_type_id("ABS-BLK"))
    env.tm.process_scan("ABS-BLK-000001", "SCANNER_HID")
    env.tm.process_scan("HOP-02", "SCANNER_HID")
    assert env.tm.state == S.VALIDATION_FAILED
    assert env.plc.alarm_calls[-1] == (True, int(NgReason.HOPPER_MISMATCH))
    assert env.tm.ctx.rows[CheckItem.HOPPER].result == CheckResult.NG
    assert env.tm.ctx.banner.level == "ng"
    assert env.plc.authorize_calls == []

    env.tm.process_scan("HOP-01", "SCANNER_HID")  # ignored while the alarm is active
    assert env.tm.state == S.VALIDATION_FAILED
    assert env.scans()[-1].result == "IGNORED"

    ok, _ = env.tm.reset_alarm()
    assert ok and env.plc.alarm_calls[-1][0] is False
    assert env.tm.state == S.BAG_SCANNED  # resume: rescan the hopper
    env.tm.process_scan("HOP-01", "SCANNER_HID")
    env.tm.process_scan("SILO-02", "SCANNER_HID")
    assert env.tm.state == S.LOADING_AUTHORIZED
    env.tm.confirm_loading_complete()
    tx = env.transactions()[-1]
    assert tx.status == "COMPLETED" and tx.ng_count == 1


def test_scan_without_bag_type_is_ignored(env):
    env.tm.process_scan("ABS-BLK-000001", "SCANNER_HID")
    assert env.tm.state == S.IDLE
    assert env.transactions() == []
    assert env.scans()[-1].result == "IGNORED"


def test_authorization_waits_for_plc(env):
    env.plc.ready = (False, "PLC not connected")
    _run_to_authorized(env)
    assert env.tm.state == S.VALIDATION_PASSED
    assert env.plc.authorize_calls == []
    assert env.tm.ctx.banner.level == "warn"
    env.plc.ready = (True, "Connected")
    env.tm.retry_authorization()
    assert env.tm.state == S.LOADING_AUTHORIZED


def test_failed_plc_write_does_not_authorize(env):
    env.plc.authorize_ok = False
    _run_to_authorized(env)
    assert env.tm.state == S.VALIDATION_PASSED
    assert "authorisation failed" in env.tm.ctx.banner.message


def test_limit_switch_blocks_authorization(env):
    silo = env.mapping.get_equipment(env.equipment_id("SILO-02"))
    data = {"equipment_code": silo.code, "name": silo.name, "category": silo.category.value, "barcode": silo.barcode,
            "capacity_kg": silo.capacity_kg, "plc_code": silo.plc_code, "ready_signal_address": "X20",
            "ready_signal_ok_value": 1, "is_active": True, "sort_order": silo.sort_order, "description": ""}
    env.mapping.save_equipment(data, silo.id)
    env.plc.signals["X20"] = 0
    _run_to_authorized(env)
    assert env.tm.state == S.VALIDATION_PASSED
    assert any("limit switch" in c.label and not c.ok for c in env.tm.ctx.readiness)
    env.plc.signals["X20"] = 1
    env.tm.retry_authorization()
    assert env.tm.state == S.LOADING_AUTHORIZED


def test_cancel_after_authorization_revokes_and_keeps_quantity(env):
    _run_to_authorized(env)
    ok, _ = env.tm.cancel("operator stopped")
    assert ok and env.tm.state == S.CANCELLED
    assert env.plc.cleared == 1
    assert env.quantity("SILO-02") == 0.0
    tx = env.transactions()[-1]
    assert tx.status == "CANCELLED" and "verify the physical state" in tx.notes
    assert env.queue() == []  # cancelled transactions are not sent by default


def test_rejected_transaction_after_ng(env):
    env.tm.select_bag_type(env.bag_type_id("ABS-BLK"))
    env.tm.process_scan("ABS-RED-000001", "SCANNER_HID")
    assert env.tm.state == S.VALIDATION_FAILED
    assert env.tm.ctx.rows[CheckItem.COLOUR].result == CheckResult.NG
    env.tm.cancel("wrong bag")
    assert env.transactions()[-1].status == "REJECTED"
    assert env.tm.ctx.alarm_active  # cancelling does not silently clear the alarm


def test_duplicate_bag_is_rejected(env):
    _run_to_authorized(env, bag="ABS-BLK-000042")
    env.tm.confirm_loading_complete()
    env.tm.process_scan("ABS-BLK-000042", "SCANNER_HID")  # auto new transaction, same bag again
    assert env.tm.state == S.VALIDATION_FAILED
    assert env.plc.alarm_calls[-1] == (True, int(NgReason.DUPLICATE_BAG))


def test_operator_cannot_reset_plc_ack_alarm(env):
    cfg = replace(env.config.plc(), alarm_reset_mode="PLC_ACK", alarm_ack_address="M50")
    env.config.save("plc", cfg)
    env.tm.select_bag_type(env.bag_type_id("ABS-BLK"))
    env.tm.process_scan("WRONG-BAG", "SCANNER_HID")
    ok, message = env.tm.reset_alarm("OPERATOR")
    assert not ok and "PLC" in message
    env.plc.alarm_acknowledged.emit()
    assert not env.tm.ctx.alarm_active and env.tm.state == S.BAG_TYPE_SELECTED


def test_plc_signal_confirmation_completes_automatically(env):
    cfg = replace(env.config.plc(), loading_confirmation_mode="PLC_SIGNAL", loading_complete_address="M100")
    env.config.save("plc", cfg)
    _run_to_authorized(env)
    ok, _ = env.tm.confirm_loading_complete()
    assert not ok  # operator confirmation not allowed in PLC mode
    env.plc.signals_updated.emit({"M100": 0})
    env.plc.signals_updated.emit({"M100": 1})
    assert env.tm.state == S.LOADING_COMPLETED
    assert env.transactions()[-1].confirmation_source == "PLC_SIGNAL"


def test_recover_interrupted_transactions(env):
    env.tm.select_bag_type(env.bag_type_id("ABS-BLK"))
    env.tm.process_scan("ABS-BLK-000001", "SCANNER_HID")
    assert env.transactions()[-1].status == "IN_PROGRESS"
    fresh = TransactionManager(env.db, env.repos, env.config, env.mapping, env.inventory, FakePlc(), env.events,
                               env.audit, FakeAuth(), env.db.path.parent / "recovery")
    assert len(fresh.recover_interrupted()) == 1
    assert env.transactions()[-1].status == "INTERRUPTED"


def test_transaction_numbers_are_unique_and_sequential(env):
    for i in range(3):
        _run_to_authorized(env, bag=f"ABS-BLK-{i:06d}")
        env.tm.confirm_loading_complete()
    numbers = [t.transaction_no for t in env.transactions()]
    assert len(set(numbers)) == 3
    assert [n[-4:] for n in numbers] == ["0001", "0002", "0003"]
    assert env.quantity("SILO-02") == 75.0
