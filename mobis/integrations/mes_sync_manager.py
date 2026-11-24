"""MES synchronisation logic (thread agnostic; used by the MES sync worker).

Rules
-----
1. Records are saved locally first and queued as PENDING (see mes_payload).
2. A cycle claims due PENDING records in batches (PENDING -> SYNCING) inside a
   write transaction, so a record can never be in two requests at once.
3. HTTP 2xx (or 409 when configured) -> SYNCED.
4. MES unreachable -> back to PENDING, retried after the retry interval,
   never counted towards FAILED (records wait for MES to come back).
5. HTTP error responses count towards ``max_retry_count``; when reached the
   record becomes FAILED.  FAILED records are never deleted and can be
   retried manually.
6. When a batch is rejected with a non-transient HTTP error, its records are
   re-sent one by one so that a single bad record cannot block the others.
7. Every record carries ``transaction_uuid`` as idempotency key.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional

from mobis.core.config_models import MesConfig
from mobis.database.database import Database
from mobis.database.repositories import Repositories
from mobis.utils.timeutil import utc_now

from .mes_client import MesClient, MesResponse
from .mes_payload import build_request_body

log = logging.getLogger("mobis.mes")

STALE_SYNCING_MINUTES = 10


@dataclass
class SyncReport:
    attempted: int = 0
    synced: int = 0
    retry: int = 0
    failed: int = 0
    online: Optional[bool] = None
    error: str = ""
    skipped_reason: str = ""


class MesSyncManager:
    MAX_BATCHES_PER_CYCLE = 20

    def __init__(
        self,
        db: Database,
        repos: Repositories,
        config: MesConfig,
        station_code: str = "",
        client_factory: Callable[[MesConfig], MesClient] = MesClient,
    ):
        self._db = db
        self._repos = repos
        self.config = config
        self.station_code = station_code
        self._client_factory = client_factory

    def apply_config(self, config: MesConfig, station_code: str) -> None:
        self.config = config
        self.station_code = station_code

    def recover(self) -> int:
        """Called at start-up: no request can be in flight, so SYNCING -> PENDING."""
        with self._db.session() as s:
            count = self._repos.mes_queue.reset_syncing(s)
        if count:
            log.warning("%d MES record(s) were left in SYNCING state and returned to PENDING", count)
        return count

    def run_once(self, force: bool = False) -> SyncReport:
        cfg = self.config
        report = SyncReport()
        if not cfg.enabled:
            report.skipped_reason = "MES integration disabled"
            return report
        if not cfg.base_url.strip():
            report.skipped_reason = "MES base URL not configured"
            return report
        client = self._client_factory(cfg)
        with self._db.session() as s:
            self._repos.mes_queue.reset_syncing(s, older_than=utc_now() - timedelta(minutes=STALE_SYNCING_MINUTES))
        attempted: set[int] = set()
        for _ in range(self.MAX_BATCHES_PER_CYCLE):
            batch_id = uuid.uuid4().hex
            with self._db.session() as s:
                rows = self._repos.mes_queue.claim_batch(
                    s, max(1, cfg.batch_size), ignore_schedule=force, batch_id=batch_id, exclude_ids=attempted
                )
                claimed = [(row.id, row.payload) for row in rows]
            if not claimed:
                break
            attempted.update(queue_id for queue_id, _payload in claimed)
            records = self._decode(claimed, report)
            if not records:
                continue
            report.attempted += len(records)
            if not self._send(client, records, batch_id, report):
                break
        return report

    # ------------------------------------------------------------------ internals
    def _decode(self, claimed: list[tuple[int, str]], report: SyncReport) -> list[tuple[int, dict[str, Any]]]:
        records, broken = [], []
        for queue_id, payload in claimed:
            try:
                records.append((queue_id, json.loads(payload)))
            except ValueError:
                broken.append(queue_id)
        if broken:
            with self._db.session() as s:
                _p, failed = self._repos.mes_queue.mark_retry(
                    s, broken, "Stored payload is not valid JSON", None, self.config.retry_interval_s,
                    count_error=True, max_errors=1,
                )
            report.failed += failed
        return records

    def _release(self, ids: list[int], error: str) -> None:
        with self._db.session() as s:
            self._repos.mes_queue.mark_retry(
                s, ids, error, None, self.config.retry_interval_s, count_error=False,
                max_errors=self.config.max_retry_count,
            )

    def _send(self, client: MesClient, records: list[tuple[int, dict[str, Any]]], batch_id: str,
              report: SyncReport) -> bool:
        """Send one batch.  Returns False when MES is unreachable (stop the cycle)."""
        cfg = self.config
        ids = [queue_id for queue_id, _record in records]
        try:
            body = build_request_body([record for _id, record in records], cfg, batch_id, self.station_code)
        except ValueError as exc:
            self._release(ids, f"Invalid MES payload configuration: {exc}")
            report.retry += len(ids)
            report.error = str(exc)
            return False
        response: MesResponse = client.send(body, batch_id)

        if response.ok:
            note = "Already processed by MES (HTTP 409)" if response.already_processed else ""
            with self._db.session() as s:
                self._repos.mes_queue.mark_synced(s, ids, response.status_code, note)
            report.synced += len(ids)
            report.online = True
            return True

        if not response.reachable:
            self._release(ids, response.error)
            report.retry += len(ids)
            report.online = False
            report.error = response.error
            return False

        report.online = True
        report.error = response.error
        if len(records) > 1 and not response.transient:
            log.warning("MES rejected batch %s (%s); retrying records individually", batch_id, response.error)
            remaining = list(records)
            while remaining:
                item = remaining.pop(0)
                if not self._send(client, [item], f"{batch_id}-{item[0]}", report):
                    if remaining:
                        self._release([queue_id for queue_id, _r in remaining], "MES became unreachable")
                        report.retry += len(remaining)
                    return False
            return True

        with self._db.session() as s:
            pending, failed = self._repos.mes_queue.mark_retry(
                s, ids, response.error, response.status_code, cfg.retry_interval_s,
                count_error=True, max_errors=cfg.max_retry_count,
            )
        report.retry += pending
        report.failed += failed
        return True
