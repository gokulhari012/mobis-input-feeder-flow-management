"""MES record and request body construction.

The company's MES API is not yet specified.  Records use neutral internal
field names; the configurable field mapping renames (or removes, with an empty
name) fields at send time, and the envelope / array layout is configurable.
``transaction_uuid`` is a stable per-record idempotency key: the MES should
treat a repeated UUID as already processed (and may answer HTTP 409).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from mobis.core.config_models import MesConfig
from mobis.core.models import TransactionStatus
from mobis.database.models import TransactionRow
from mobis.database.repositories import Repositories
from mobis.utils.timeutil import to_iso, utc_now

SOURCE_SYSTEM = "MOBIS-IFFM"


def build_transaction_record(tx: TransactionRow) -> dict[str, Any]:
    return {
        "transaction_id": tx.transaction_no,
        "transaction_uuid": tx.transaction_uuid,
        "station_code": tx.station_code,
        "status": tx.status,
        "validation_status": tx.validation_status,
        "operator": tx.operator_username,
        "bag_type_code": tx.bag_type_code,
        "bag_type_name": tx.bag_type_name,
        "material_type": tx.material_type,
        "colour": tx.colour,
        "bag_barcode": tx.bag_barcode,
        "bag_lot": tx.bag_lot,
        "bag_serial": tx.bag_serial,
        "bag_weight_kg": tx.bag_weight_kg,
        "hopper_code": tx.hopper_code,
        "silo_code": tx.silo_code,
        "distribution_tank_code": tx.distribution_tank_code,
        "mold_code": tx.mold_code,
        "destination_code": tx.destination_code,
        "quantity_before_kg": tx.quantity_before_kg,
        "quantity_after_kg": tx.quantity_after_kg,
        "ng_count": tx.ng_count,
        "confirmation_source": tx.confirmation_source,
        "started_at": to_iso(tx.started_at),
        "validated_at": to_iso(tx.validated_at),
        "authorized_at": to_iso(tx.authorized_at),
        "completed_at": to_iso(tx.completed_at),
        "ended_at": to_iso(tx.ended_at),
    }


def should_queue(status: str, config: MesConfig) -> bool:
    if status == TransactionStatus.COMPLETED.value:
        return True
    if status == TransactionStatus.REJECTED.value:
        return config.include_rejected
    if status == TransactionStatus.CANCELLED.value:
        return config.include_cancelled
    return False


def enqueue_transaction(session: Session, repos: Repositories, tx: TransactionRow, config: MesConfig) -> bool:
    """Queue a finished transaction for MES synchronisation (caller's unit of work)."""
    if not should_queue(tx.status, config):
        return False
    record = build_transaction_record(tx)
    repos.mes_queue.enqueue(session, tx.id, tx.transaction_uuid, json.dumps(record, ensure_ascii=False))
    return True


def parse_field_mapping(text: str) -> dict[str, str]:
    if not (text or "").strip():
        return {}
    mapping = json.loads(text)
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise ValueError("Field mapping must be a JSON object of the form {\"internal_field\": \"mesField\"}")
    return mapping


def apply_field_mapping(record: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    result = {}
    for key, value in record.items():
        target = mapping.get(key, key)
        if target:  # empty name excludes the field
            result[target] = value
    return result


def build_request_body(
    records: list[dict[str, Any]], config: MesConfig, batch_id: str, station_code: str = ""
) -> Any:
    mapping = parse_field_mapping(config.field_mapping_json)
    mapped = [apply_field_mapping(r, mapping) for r in records]
    if config.payload_mode == "ARRAY":
        return mapped
    return {
        "source": SOURCE_SYSTEM,
        "station_code": station_code,
        "batch_id": batch_id,
        "sent_at": to_iso(utc_now()),
        "record_count": len(mapped),
        (config.records_key or "records"): mapped,
    }


def sample_record(station_code: str = "IFM01") -> dict[str, Any]:
    """Example record (used for the payload preview)."""
    now = to_iso(utc_now())
    return {
        "transaction_id": f"{station_code}-20261124-0001",
        "transaction_uuid": "00000000-0000-0000-0000-000000000000",
        "station_code": station_code,
        "status": "COMPLETED",
        "validation_status": "OK",
        "operator": "",
        "bag_type_code": "ABS-BLK",
        "bag_type_name": "ABS - Black",
        "material_type": "ABS",
        "colour": "Black",
        "bag_barcode": "ABS-BLK-000001",
        "bag_lot": "",
        "bag_serial": "",
        "bag_weight_kg": 25.0,
        "hopper_code": "HOP-01",
        "silo_code": "SILO-02",
        "distribution_tank_code": "",
        "mold_code": "",
        "destination_code": "SILO-02",
        "quantity_before_kg": 1000.0,
        "quantity_after_kg": 1025.0,
        "ng_count": 0,
        "confirmation_source": "PLC_SIGNAL",
        "started_at": now,
        "validated_at": now,
        "authorized_at": now,
        "completed_at": now,
        "ended_at": now,
    }


def preview_body(config: MesConfig, station_code: str, records: Optional[list[dict[str, Any]]] = None) -> str:
    body = build_request_body(records or [sample_record(station_code)], config, "preview-batch", station_code)
    return json.dumps(body, indent=2, ensure_ascii=False)
