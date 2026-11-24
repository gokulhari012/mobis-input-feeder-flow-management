"""SQLAlchemy ORM mapping of the schema created by the migrations.

The schema itself is owned by :mod:`mobis.database.migrations`; these classes
must mirror the latest migration (a unit test verifies every ORM column exists
in a freshly migrated database).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from mobis.core.models import BagType, BarcodeMatchType, Equipment, EquipmentCategory, EquipmentLink
from mobis.utils.timeutil import from_iso, to_iso


class UtcDateTime(TypeDecorator):
    """Stores aware datetimes as ISO-8601 UTC strings."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or isinstance(value, str):
            return value
        return to_iso(value)

    def process_result_value(self, value, dialect):
        return from_iso(value) if value else None


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
class EquipmentRow(Base):
    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    barcode: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String, default="")
    capacity_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    plc_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ready_signal_address: Mapped[str] = mapped_column(String, default="")
    ready_signal_ok_value: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    def to_domain(self) -> Equipment:
        return Equipment(
            id=self.id,
            code=self.equipment_code,
            name=self.name,
            category=EquipmentCategory(self.category),
            barcode=self.barcode,
            description=self.description or "",
            capacity_kg=self.capacity_kg,
            plc_code=self.plc_code,
            ready_signal_address=self.ready_signal_address or "",
            ready_signal_ok_value=self.ready_signal_ok_value if self.ready_signal_ok_value is not None else 1,
            is_active=bool(self.is_active),
            sort_order=self.sort_order or 0,
        )


class EquipmentMappingRow(Base):
    __tablename__ = "equipment_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_equipment_id: Mapped[int] = mapped_column(Integer)
    target_equipment_id: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    def to_domain(self) -> EquipmentLink:
        return EquipmentLink(
            id=self.id,
            source_id=self.source_equipment_id,
            target_id=self.target_equipment_id,
            is_active=bool(self.is_active),
            description=self.description or "",
        )


class BagTypeRow(Base):
    __tablename__ = "bag_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    material_type: Mapped[str] = mapped_column(String)
    colour: Mapped[str] = mapped_column(String)
    colour_code: Mapped[str] = mapped_column(String, default="")
    barcode_match_type: Mapped[str] = mapped_column(String, default="PREFIX")
    barcode_match_value: Mapped[str] = mapped_column(String, default="")
    weight_kg: Mapped[float] = mapped_column(Float)
    hopper_id: Mapped[int] = mapped_column(Integer)
    silo_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    silo_scan_required: Mapped[bool] = mapped_column(Boolean, default=False)
    distribution_tank_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    distribution_scan_required: Mapped[bool] = mapped_column(Boolean, default=False)
    mold_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mold_scan_required: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(String, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    def to_domain(self) -> BagType:
        return BagType(
            id=self.id,
            code=self.code,
            name=self.name,
            material_type=self.material_type,
            colour=self.colour,
            colour_code=self.colour_code or "",
            match_type=BarcodeMatchType(self.barcode_match_type),
            match_value=self.barcode_match_value or "",
            weight_kg=float(self.weight_kg),
            hopper_id=self.hopper_id,
            silo_id=self.silo_id,
            silo_scan_required=bool(self.silo_scan_required),
            distribution_tank_id=self.distribution_tank_id,
            distribution_scan_required=bool(self.distribution_scan_required),
            mold_id=self.mold_id,
            mold_scan_required=bool(self.mold_scan_required),
            description=self.description or "",
            is_active=bool(self.is_active),
        )


# --------------------------------------------------------------------------- #
class TransactionRow(Base):
    __tablename__ = "material_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_no: Mapped[str] = mapped_column(String)
    transaction_uuid: Mapped[str] = mapped_column(String)
    station_code: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    process_state: Mapped[str] = mapped_column(String)
    validation_status: Mapped[str] = mapped_column(String, default="PENDING")
    operator_username: Mapped[str] = mapped_column(String, default="")
    bag_type_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bag_type_code: Mapped[str] = mapped_column(String)
    bag_type_name: Mapped[str] = mapped_column(String)
    material_type: Mapped[str] = mapped_column(String)
    colour: Mapped[str] = mapped_column(String)
    bag_barcode: Mapped[str] = mapped_column(String, default="")
    bag_lot: Mapped[str] = mapped_column(String, default="")
    bag_serial: Mapped[str] = mapped_column(String, default="")
    bag_weight_kg: Mapped[float] = mapped_column(Float, default=0.0)
    hopper_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hopper_code: Mapped[str] = mapped_column(String, default="")
    silo_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    silo_code: Mapped[str] = mapped_column(String, default="")
    distribution_tank_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    distribution_tank_code: Mapped[str] = mapped_column(String, default="")
    mold_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mold_code: Mapped[str] = mapped_column(String, default="")
    destination_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    destination_code: Mapped[str] = mapped_column(String, default="")
    quantity_before_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quantity_after_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bag_result: Mapped[str] = mapped_column(String, default="PENDING")
    colour_result: Mapped[str] = mapped_column(String, default="PENDING")
    hopper_result: Mapped[str] = mapped_column(String, default="PENDING")
    silo_result: Mapped[str] = mapped_column(String, default="PENDING")
    distribution_result: Mapped[str] = mapped_column(String, default="PENDING")
    mold_result: Mapped[str] = mapped_column(String, default="PENDING")
    ng_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String, default="")
    confirmation_source: Mapped[str] = mapped_column(String, default="")
    notes: Mapped[str] = mapped_column(String, default="")
    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    validated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    authorized_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    def to_dict(self) -> dict[str, Any]:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class BarcodeScanRow(Base):
    __tablename__ = "barcode_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(UtcDateTime)
    stage: Mapped[str] = mapped_column(String)
    barcode: Mapped[str] = mapped_column(String)
    input_source: Mapped[str] = mapped_column(String)
    result: Mapped[str] = mapped_column(String)
    expected_value: Mapped[str] = mapped_column(String, default="")
    matched_equipment_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(String, default="")
    operator_username: Mapped[str] = mapped_column(String, default="")

    def to_dict(self) -> dict[str, Any]:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


# --------------------------------------------------------------------------- #
class TankInventoryRow(Base):
    __tablename__ = "tank_inventory"

    equipment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quantity_kg: Mapped[float] = mapped_column(Float, default=0.0)
    last_transaction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_change_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_correction_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class InventoryHistoryRow(Base):
    __tablename__ = "inventory_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(Integer)
    changed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    change_type: Mapped[str] = mapped_column(String)
    quantity_before_kg: Mapped[float] = mapped_column(Float)
    change_kg: Mapped[float] = mapped_column(Float)
    quantity_after_kg: Mapped[float] = mapped_column(Float)
    transaction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String, default="")
    username: Mapped[str] = mapped_column(String, default="")

    def to_dict(self) -> dict[str, Any]:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


# --------------------------------------------------------------------------- #
class _ConfigColumns:
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_by: Mapped[str] = mapped_column(String, default="")


class PlcConfigRow(_ConfigColumns, Base):
    __tablename__ = "plc_configuration"


class ScannerConfigRow(_ConfigColumns, Base):
    __tablename__ = "scanner_configuration"


class MesConfigRow(_ConfigColumns, Base):
    __tablename__ = "mes_configuration"


class AppSettingRow(_ConfigColumns, Base):
    __tablename__ = "app_settings"


# --------------------------------------------------------------------------- #
class MesQueueRow(Base):
    __tablename__ = "mes_sync_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String, default="")
    last_http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    batch_id: Mapped[str] = mapped_column(String, default="")
    synced_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    def to_dict(self) -> dict[str, Any]:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class UserRow(Base):
    __tablename__ = "application_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String)
    full_name: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String)
    password_hash: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)
    username: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    entity: Mapped[str] = mapped_column(String, default="")
    entity_id: Mapped[str] = mapped_column(String, default="")
    details: Mapped[str] = mapped_column(String, default="")
    old_value: Mapped[str] = mapped_column(String, default="")
    new_value: Mapped[str] = mapped_column(String, default="")

    def to_dict(self) -> dict[str, Any]:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class SystemEventRow(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)
    severity: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    details: Mapped[str] = mapped_column(String, default="")
    transaction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}
