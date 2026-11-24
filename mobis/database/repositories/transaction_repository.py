from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from mobis.core.models import TransactionStatus
from mobis.database.models import BarcodeScanRow, MesQueueRow, TransactionRow
from mobis.utils.timeutil import utc_now


@dataclass
class TransactionFilter:
    date_from: Optional[datetime] = None   # inclusive, UTC
    date_to: Optional[datetime] = None     # exclusive, UTC
    bag_type_code: str = ""
    colour: str = ""
    hopper_code: str = ""
    silo_code: str = ""
    distribution_tank_code: str = ""
    status: str = ""
    validation_status: str = ""
    mes_status: str = ""                   # PENDING/SYNCING/SYNCED/FAILED/NOT_QUEUED
    text: str = ""                         # barcode / transaction no / lot search


class TransactionRepository:
    # ------------------------------------------------------------------ create / update
    def next_transaction_no(self, s: Session, station_code: str, local_date: str) -> str:
        prefix = f"{station_code}-{local_date}-"
        numbers = s.scalars(
            select(TransactionRow.transaction_no).where(TransactionRow.transaction_no.startswith(prefix, autoescape=True))
        ).all()
        highest = 0
        for number in numbers:
            tail = number[len(prefix):]
            if tail.isdigit():
                highest = max(highest, int(tail))
        return f"{prefix}{highest + 1:04d}"

    def create(self, s: Session, **fields: Any) -> TransactionRow:
        now = utc_now()
        fields.setdefault("started_at", now)
        fields.setdefault("updated_at", now)
        row = TransactionRow(**fields)
        s.add(row)
        s.flush()
        return row

    def get(self, s: Session, transaction_id: int) -> Optional[TransactionRow]:
        return s.get(TransactionRow, transaction_id)

    def update(self, s: Session, transaction_id: int, **fields: Any) -> TransactionRow:
        row = s.get(TransactionRow, transaction_id)
        if row is None:
            raise KeyError(f"Transaction {transaction_id} not found")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()
        s.flush()
        return row

    def add_scan(self, s: Session, **fields: Any) -> BarcodeScanRow:
        fields.setdefault("scanned_at", utc_now())
        row = BarcodeScanRow(**fields)
        s.add(row)
        s.flush()
        return row

    def mark_interrupted(self, s: Session) -> list[str]:
        """Close transactions left IN_PROGRESS by an unexpected shutdown."""
        now = utc_now()
        rows = s.scalars(
            select(TransactionRow).where(TransactionRow.status == TransactionStatus.IN_PROGRESS.value)
        ).all()
        for row in rows:
            row.status = TransactionStatus.INTERRUPTED.value
            row.ended_at = now
            row.updated_at = now
            note = (
                f"Interrupted in state {row.process_state} (application stopped). "
                "Quantity not updated - verify physical state."
            )
            row.notes = f"{row.notes}\n{note}".strip()
        return [row.transaction_no for row in rows]

    # ------------------------------------------------------------------ queries
    def find_completed_by_bag_barcode(self, s: Session, barcode: str) -> Optional[TransactionRow]:
        return s.scalars(
            select(TransactionRow)
            .where(
                TransactionRow.status == TransactionStatus.COMPLETED.value,
                TransactionRow.bag_barcode.collate("NOCASE") == barcode,
            )
            .order_by(TransactionRow.completed_at.desc())
        ).first()

    def scans_for(self, s: Session, transaction_id: int) -> list[dict[str, Any]]:
        rows = s.scalars(
            select(BarcodeScanRow).where(BarcodeScanRow.transaction_id == transaction_id).order_by(BarcodeScanRow.id)
        )
        return [row.to_dict() for row in rows]

    def _apply_filter(self, query, f: TransactionFilter):
        T = TransactionRow
        if f.date_from:
            query = query.where(T.started_at >= f.date_from)
        if f.date_to:
            query = query.where(T.started_at < f.date_to)
        if f.bag_type_code:
            query = query.where(T.bag_type_code == f.bag_type_code)
        if f.colour:
            query = query.where(T.colour.collate("NOCASE") == f.colour)
        if f.hopper_code:
            query = query.where(T.hopper_code == f.hopper_code)
        if f.silo_code:
            query = query.where(T.silo_code == f.silo_code)
        if f.distribution_tank_code:
            query = query.where(T.distribution_tank_code == f.distribution_tank_code)
        if f.status:
            query = query.where(T.status == f.status)
        if f.validation_status:
            query = query.where(T.validation_status == f.validation_status)
        if f.mes_status == "NOT_QUEUED":
            query = query.where(MesQueueRow.id.is_(None))
        elif f.mes_status:
            query = query.where(MesQueueRow.status == f.mes_status)
        text = f.text.strip()
        if text:
            query = query.where(
                or_(
                    T.bag_barcode.contains(text, autoescape=True),
                    T.transaction_no.contains(text, autoescape=True),
                    T.bag_lot.contains(text, autoescape=True),
                    T.bag_serial.contains(text, autoescape=True),
                )
            )
        return query

    def _base_query(self, f: TransactionFilter):
        query = select(TransactionRow, MesQueueRow.status).outerjoin(
            MesQueueRow, MesQueueRow.transaction_id == TransactionRow.id
        )
        return self._apply_filter(query, f).order_by(TransactionRow.started_at.desc(), TransactionRow.id.desc())

    def search(self, s: Session, f: TransactionFilter, limit: int = 2000) -> list[dict[str, Any]]:
        rows = s.execute(self._base_query(f).limit(limit))
        return [dict(tx.to_dict(), mes_status=status or "NOT_QUEUED") for tx, status in rows]

    def iter_search(self, s: Session, f: TransactionFilter, chunk: int = 1000) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            rows = s.execute(self._base_query(f).limit(chunk).offset(offset)).all()
            if not rows:
                return
            for tx, status in rows:
                yield dict(tx.to_dict(), mes_status=status or "NOT_QUEUED")
            offset += chunk

    def distinct_values(self, s: Session, column: str) -> list[str]:
        col = getattr(TransactionRow, column)
        return [v for v in s.scalars(select(col).distinct().order_by(col)) if v]

    def search_scans(
        self,
        s: Session,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        result: str = "",
        text: str = "",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        query = select(BarcodeScanRow, TransactionRow.transaction_no).outerjoin(
            TransactionRow, TransactionRow.id == BarcodeScanRow.transaction_id
        )
        if date_from:
            query = query.where(BarcodeScanRow.scanned_at >= date_from)
        if date_to:
            query = query.where(BarcodeScanRow.scanned_at < date_to)
        if result:
            query = query.where(BarcodeScanRow.result == result)
        if text.strip():
            t = text.strip()
            query = query.where(
                or_(
                    BarcodeScanRow.barcode.contains(t, autoescape=True),
                    BarcodeScanRow.message.contains(t, autoescape=True),
                    TransactionRow.transaction_no.contains(t, autoescape=True),
                )
            )
        query = query.order_by(BarcodeScanRow.scanned_at.desc(), BarcodeScanRow.id.desc()).limit(limit)
        return [dict(scan.to_dict(), transaction_no=no or "") for scan, no in s.execute(query)]
