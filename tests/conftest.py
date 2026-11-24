from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal  # noqa: E402

from mobis.core.config_models import BarcodeFormatConfig  # noqa: E402
from mobis.core.models import BagType, BarcodeMatchType, Equipment, EquipmentCategory  # noqa: E402
from mobis.core.validation_engine import ConfigSnapshot  # noqa: E402
from mobis.database.database import Database  # noqa: E402
from mobis.database.migrations import MigrationRunner  # noqa: E402
from mobis.database.seed import seed_initial_data  # noqa: E402

H, S, D, M = (EquipmentCategory.HOPPER, EquipmentCategory.SILO,
              EquipmentCategory.DISTRIBUTION_TANK, EquipmentCategory.MOLD)


@pytest.fixture(scope="session")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    MigrationRunner(database.engine).upgrade()
    yield database
    database.dispose()


@pytest.fixture
def seeded_db(db):
    seed_initial_data(db, include_demo_bag_types=True)
    return db


# --------------------------------------------------------------------------- #
# Pure snapshot reproducing the physical setup of the specification
# --------------------------------------------------------------------------- #
def make_equipment():
    items = [
        Equipment(1, "HOP-01", "Hopper 1", H, "HOP-01"),
        Equipment(2, "HOP-02", "Hopper 2", H, "HOP-02"),
        Equipment(3, "HOP-03", "Hopper 3", H, "HOP-03"),
        Equipment(4, "HOP-04", "Hopper 4", H, "HOP-04"),
        Equipment(11, "SILO-01", "Silo 1", S, "SILO-01", capacity_kg=14000),
        Equipment(12, "SILO-02", "Silo 2", S, "SILO-02", capacity_kg=14000),
        Equipment(13, "SILO-03", "Silo 3", S, "SILO-03", capacity_kg=14000),
        Equipment(21, "DT-01", "Distribution Tank 1", D, "DT-01", capacity_kg=4000),
        Equipment(22, "DT-02", "Distribution Tank 2", D, "DT-02", capacity_kg=4000),
        Equipment(23, "DT-03", "Distribution Tank 3", D, "DT-03", capacity_kg=4000),
        Equipment(24, "DT-04", "Distribution Tank 4", D, "DT-04", capacity_kg=4000),
        Equipment(31, "MLD-01", "Mold 1", M, "MLD-01"),
        Equipment(32, "MLD-02", "Mold 2", M, "MLD-02"),
        Equipment(99, "HOP-99", "Old Hopper", H, "HOP-99", is_active=False),
    ]
    return {e.id: e for e in items}


LINKS = frozenset({(1, 12), (2, 11), (3, 22), (3, 23), (3, 24), (4, 21), (1, 31)})


def make_bag_types():
    return {
        1: BagType(1, "ABS-BLK", "ABS - Black", "ABS", "Black", "BLK", BarcodeMatchType.PREFIX, "ABS-BLK-", 25.0,
                   hopper_id=1, silo_id=12, silo_scan_required=True),
        2: BagType(2, "PC-GRY", "PC - Grey", "PC", "Grey", "GRY", BarcodeMatchType.PREFIX, "PC-GRY-", 25.0,
                   hopper_id=3, distribution_tank_id=22, distribution_scan_required=True),
        3: BagType(3, "ABS-RED", "ABS - Red", "ABS", "Red", "RED", BarcodeMatchType.PREFIX, "ABS-RED-", 20.0,
                   hopper_id=4, distribution_tank_id=21, distribution_scan_required=True),
        4: BagType(4, "PA6-BLK", "PA6 - Black", "PA6", "Black", "BLK", BarcodeMatchType.PREFIX, "PA6-BLK-", 25.0,
                   hopper_id=3, distribution_tank_id=23, distribution_scan_required=True),
    }


def make_snapshot(**overrides) -> ConfigSnapshot:
    data = dict(
        equipment=make_equipment(),
        links=LINKS,
        bag_types=make_bag_types(),
        barcode_format=BarcodeFormatConfig(),
        mold_verification_enabled=False,
    )
    data.update(overrides)
    return ConfigSnapshot(**data)


@pytest.fixture
def snapshot():
    return make_snapshot()


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakePlc(QObject):
    signals_updated = Signal(object)
    connection_changed = Signal(bool, str)
    alarm_acknowledged = Signal()

    def __init__(self):
        super().__init__()
        self.ready = (True, "Connected (fake)")
        self.alarm_calls: list[tuple[bool, int]] = []
        self.authorize_calls: list[tuple[int, int]] = []
        self.cleared = 0
        self.signals: dict[str, int] = {}
        self.auto_respond = True
        self.authorize_ok = True
        self.pending_callbacks = []

    @property
    def alarm_active(self) -> bool:
        return bool(self.alarm_calls) and self.alarm_calls[-1][0]

    def is_ready(self):
        return self.ready

    def get_signal(self, address):
        return self.signals.get(self.normalize_address(address))

    def signal_values(self):
        return dict(self.signals)

    def normalize_address(self, address):
        return address.strip().upper()

    def set_alarm(self, active, reason_code=0):
        self.alarm_calls.append((active, reason_code))

    def authorize_loading(self, hopper_code, destination_code, callback):
        self.authorize_calls.append((hopper_code, destination_code))
        if self.auto_respond:
            callback(self.authorize_ok, "" if self.authorize_ok else "simulated write failure")
        else:
            self.pending_callbacks.append(callback)

    def clear_authorization(self):
        self.cleared += 1


class FakeAuth:
    def record_username(self):
        return "op1"

    def audit_username(self):
        return "op1"
