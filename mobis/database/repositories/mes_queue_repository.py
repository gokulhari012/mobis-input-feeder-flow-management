from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mobis.core.models import MesSyncStatus
from mobis.database.models import MesQueueRow, TransactionRow
from mobis.utils.timeutil import utc_now

P, SY, OK, F = (MesSyncStatus.PENDING.value, MesSyncStatus.SYNCING.value,
                MesSyncStatus.SYNCED.value, MesSyncStatus.FAILED.value)


class MesQueueRepository:
    def enqueue(self, s: Session, transaction_id: int, idempotency_key: str, payload_json: str) -> MesQueueRow:
        now = utc_now()
        row = MesQueueRow(
            transaction_id=transaction_id,
            idempotency_key=idempotency_key,
            payload=payload_json,
            status=P,
            attempt_count=0,
            error_count=0,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        s.flush()
        return row

    def claim_batch(
        self,
        s: Session,
        limit: int,
        now: Optional[datetime] = None,
        ignore_schedule: bool = False,
        batch_id: str = "",
        exclude_ids: Iterable[int] = (),
    ) -> list[MesQueueRow]:
        """Atomically move up to ``limit`` due PENDING records to SYNCING.

        Must run inside a write session (BEGIN IMMEDIATE) so that two sync
        cycles can never claim the same record - this, together with the
        per-record idempotency key, prevents duplicate MES submissions.
        ``exclude_ids`` prevents re-claiming records already attempted in the
        current cycle (otherwise a forced cycle could exhaust all retries at once).
        """
        now = now or utc_now()
        query = select(MesQueueRow).where(MesQueueRow.status == P)
        exclude = list(exclude_ids)
        if exclude:
            query = query.where(MesQueueRow.id.not_in(exclude))
        if not ignore_schedule:
            query = query.where(or_(MesQueueRow.next_attempt_at.is_(None), MesQueueRow.next_attempt_at <= now))
        rows = list(s.scalars(query.order_by(MesQueueRow.id).limit(max(1, limit))))
        for row in rows:
            row.status = SY
            row.last_attempt_at = now
            row.batch_id = batch_id
            row.updated_at = now
        s.flush()
        return rows

    def mark_synced(
        self, s: Session, ids: Iterable[int], http_status: Optional[int], note: str = "", now: Optional[datetime] = None
    ) -> int:
        now = now or utc_now()
        count = 0
        for row in s.scalars(select(MesQueueRow).where(MesQueueRow.id.in_(list(ids)))):
            row.status = OK
            row.attempt_count += 1
            row.synced_at = now
            row.last_http_status = http_status
            row.last_error = note
            row.next_attempt_at = None
            row.updated_at = now
            count += 1
        return count

    def mark_retry(
        self,
        s: Session,
        ids: Iterable[int],
        error: str,
        http_status: Optional[int],
        retry_interval_s: float,
        count_error: bool,
        max_errors: int,
        now: Optional[datetime] = None,
    ) -> tuple[int, int]:
        """Return records to PENDING (or FAILED once ``max_errors`` HTTP errors are reached).

        Network failures (MES unreachable) use ``count_error=False``: such
        records stay PENDING indefinitely and are retried when MES is back.
        """
        now = now or utc_now()
        pending = failed = 0
        for row in s.scalars(select(MesQueueRow).where(MesQueueRow.id.in_(list(ids)))):
            row.attempt_count += 1
            if count_error:
                row.error_count += 1
            row.last_error = error[:2000]
            row.last_http_status = http_status
            row.updated_at = now
            if count_error and max_errors > 0 and row.error_count >= max_errors:
                row.status = F
                row.next_attempt_at = None
                failed += 1
            else:
                row.status = P
                row.next_attempt_at = now + timedelta(seconds=max(1.0, retry_interval_s))
                pending += 1
        return pending, failed

    def reset_syncing(self, s: Session, older_than: Optional[datetime] = None) -> int:
        """Recover records stuck in SYNCING (e.g. after a crash during a request)."""
        query = select(MesQueueRow).where(MesQueueRow.status == SY)
        if older_than is not None:
            query = query.where(or_(MesQueueRow.last_attempt_at.is_(None), MesQueueRow.last_attempt_at < older_than))
        rows = list(s.scalars(query))
        now = utc_now()
        for row in rows:
            row.status = P
            row.next_attempt_at = None
            row.updated_at = now
        return len(rows)

    def retry_failed(self, s: Session, ids: Optional[Iterable[int]] = None) -> int:
        query = select(MesQueueRow).where(MesQueueRow.status == F)
        if ids is not None:
            query = query.where(MesQueueRow.id.in_(list(ids)))
        rows = list(s.scalars(query))
        now = utc_now()
        for row in rows:
            row.status = P
            row.error_count = 0
            row.next_attempt_at = None
            row.updated_at = now
        return len(rows)

    def counts(self, s: Session) -> dict[str, int]:
        result = {status.value: 0 for status in MesSyncStatus}
        for status, count in s.execute(select(MesQueueRow.status, func.count()).group_by(MesQueueRow.status)):
            result[status] = int(count)
        result["TOTAL"] = sum(result[status.value] for status in MesSyncStatus)
        return result

    def last_synced_at(self, s: Session) -> Optional[datetime]:
        return s.scalar(select(func.max(MesQueueRow.synced_at)))

    def get(self, s: Session, queue_id: int) -> Optional[MesQueueRow]:
        return s.get(MesQueueRow, queue_id)

    def list(self, s: Session, status: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        query = select(MesQueueRow, TransactionRow.transaction_no).join(
            TransactionRow, TransactionRow.id == MesQueueRow.transaction_id
        )
        if status:
            query = query.where(MesQueueRow.status == status)
        query = query.order_by(MesQueueRow.id.desc()).limit(limit)
        return [dict(row.to_dict(), transaction_no=no) for row, no in s.execute(query)]
