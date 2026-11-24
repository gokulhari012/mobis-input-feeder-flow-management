"""MES synchronisation: queue states, retries, isolation and idempotency."""

from __future__ import annotations

import json
import uuid

from mobis.core.config_models import MesConfig
from mobis.database.repositories import Repositories
from mobis.integrations.mes_client import MesResponse
from mobis.integrations.mes_payload import apply_field_mapping, build_request_body, enqueue_transaction
from mobis.integrations.mes_sync_manager import MesSyncManager


class FakeClient:
    """Records sent batches; behaviour configurable per call."""

    def __init__(self, behaviour="ok", bad_ids=()):
        self.behaviour = behaviour
        self.bad_ids = set(bad_ids)
        self.batches: list[list[str]] = []

    def __call__(self, _cfg):  # used as client factory
        return self

    def send(self, body, request_id):
        records = body["records"] if isinstance(body, dict) else body
        ids = [r["transaction_id"] for r in records]
        self.batches.append(ids)
        if self.behaviour == "unreachable":
            return MesResponse(False, None, reachable=False, error="MES unreachable: connection refused")
        if self.behaviour == "server_error":
            return MesResponse(False, 500, True, "HTTP 500: boom")
        if self.behaviour == "conflict":
            return MesResponse(True, 409, already_processed=True)
        if self.bad_ids & set(ids):
            return MesResponse(False, 400, True, "HTTP 400: invalid record")
        return MesResponse(True, 200)


def _config(**kw) -> MesConfig:
    values = dict(enabled=True, base_url="http://mes.invalid", batch_size=2, retry_interval_s=60, max_retry_count=3)
    values.update(kw)
    return MesConfig(**values)


def _queue_transactions(db, repos, count, status="COMPLETED"):
    numbers = []
    with db.session() as s:
        for i in range(count):
            number = f"IFM01-20261124-{i + 1:04d}"
            tx = repos.transactions.create(
                s, transaction_no=number, transaction_uuid=str(uuid.uuid4()), station_code="IFM01",
                status=status, process_state="LOADING_COMPLETED", validation_status="OK",
                bag_type_code="ABS-BLK", bag_type_name="ABS - Black", material_type="ABS", colour="Black",
                bag_barcode=f"ABS-BLK-{i:06d}", bag_weight_kg=25.0,
            )
            enqueue_transaction(s, repos, tx, _config())
            numbers.append(number)
    return numbers


def _statuses(db, repos):
    with db.read_session() as s:
        return {r["transaction_no"]: r for r in repos.mes_queue.list(s)}


def test_successful_sync_marks_records_synced_in_batches(db):
    repos = Repositories()
    _queue_transactions(db, repos, 5)
    client = FakeClient()
    report = MesSyncManager(db, repos, _config(), "IFM01", client).run_once()
    assert report.synced == 5 and report.online is True
    assert [len(b) for b in client.batches] == [2, 2, 1]
    assert {r["status"] for r in _statuses(db, repos).values()} == {"SYNCED"}


def test_unreachable_mes_keeps_records_pending_forever(db):
    repos = Repositories()
    _queue_transactions(db, repos, 3)
    manager = MesSyncManager(db, repos, _config(max_retry_count=2), "IFM01", FakeClient("unreachable"))
    for _ in range(5):
        report = manager.run_once(force=True)
        assert report.online is False
    rows = list(_statuses(db, repos).values())
    assert {r["status"] for r in rows} == {"PENDING"}
    # Unreachable MES is not the records' fault: no error counted, nothing becomes FAILED.
    assert all(r["error_count"] == 0 for r in rows)
    assert sum(r["attempt_count"] for r in rows) >= 5


def test_http_errors_lead_to_failed_after_max_retries_and_are_never_deleted(db):
    repos = Repositories()
    _queue_transactions(db, repos, 1)
    manager = MesSyncManager(db, repos, _config(max_retry_count=3), "IFM01", FakeClient("server_error"))
    for _ in range(2):
        manager.run_once(force=True)
    assert next(iter(_statuses(db, repos).values()))["status"] == "PENDING"
    report = manager.run_once(force=True)
    assert report.failed == 1
    row = next(iter(_statuses(db, repos).values()))
    assert row["status"] == "FAILED" and row["error_count"] == 3
    # FAILED records are not picked up again and are kept.
    assert manager.run_once(force=True).attempted == 0
    with db.read_session() as s:
        assert repos.mes_queue.counts(s)["TOTAL"] == 1


def test_retry_failed_resets_records(db):
    repos = Repositories()
    _queue_transactions(db, repos, 1)
    manager = MesSyncManager(db, repos, _config(max_retry_count=1), "IFM01", FakeClient("server_error"))
    manager.run_once(force=True)
    with db.session() as s:
        assert repos.mes_queue.retry_failed(s) == 1
    row = next(iter(_statuses(db, repos).values()))
    assert row["status"] == "PENDING" and row["error_count"] == 0
    manager._client_factory = FakeClient("ok")
    assert manager.run_once().synced == 1


def test_conflict_is_treated_as_already_synced(db):
    repos = Repositories()
    _queue_transactions(db, repos, 2)
    MesSyncManager(db, repos, _config(), "IFM01", FakeClient("conflict")).run_once()
    rows = _statuses(db, repos).values()
    assert {r["status"] for r in rows} == {"SYNCED"}
    assert all("409" in r["last_error"] for r in rows)


def test_rejected_batch_is_isolated_record_by_record(db):
    repos = Repositories()
    numbers = _queue_transactions(db, repos, 2)
    client = FakeClient(bad_ids=[numbers[1]])
    MesSyncManager(db, repos, _config(batch_size=10), "IFM01", client).run_once()
    rows = _statuses(db, repos)
    assert rows[numbers[0]]["status"] == "SYNCED"
    assert rows[numbers[1]]["status"] == "PENDING" and rows[numbers[1]]["error_count"] == 1
    assert client.batches == [numbers, [numbers[0]], [numbers[1]]]


def test_claimed_records_are_not_claimed_twice(db):
    repos = Repositories()
    _queue_transactions(db, repos, 2)
    with db.session() as s:
        first = repos.mes_queue.claim_batch(s, 10)
    with db.session() as s:
        second = repos.mes_queue.claim_batch(s, 10, ignore_schedule=True)
    assert len(first) == 2 and second == []
    # Start-up recovery returns interrupted SYNCING records to PENDING (no duplicates, no loss).
    assert MesSyncManager(db, repos, _config()).recover() == 2
    assert {r["status"] for r in _statuses(db, repos).values()} == {"PENDING"}


def test_retry_respects_schedule_unless_forced(db):
    repos = Repositories()
    _queue_transactions(db, repos, 1)
    manager = MesSyncManager(db, repos, _config(retry_interval_s=3600), "IFM01", FakeClient("server_error"))
    assert manager.run_once().attempted == 1
    assert manager.run_once().attempted == 0        # waiting for retry interval
    assert manager.run_once(force=True).attempted == 1  # "Sync Now"


def test_disabled_integration_is_skipped_and_records_stay_queued(db):
    repos = Repositories()
    _queue_transactions(db, repos, 1)
    report = MesSyncManager(db, repos, _config(enabled=False), "IFM01", FakeClient()).run_once()
    assert report.skipped_reason and report.attempted == 0
    assert next(iter(_statuses(db, repos).values()))["status"] == "PENDING"


def test_only_completed_transactions_are_queued_by_default(db):
    repos = Repositories()
    _queue_transactions(db, repos, 1, status="CANCELLED")
    with db.read_session() as s:
        assert repos.mes_queue.counts(s)["TOTAL"] == 0


def test_payload_field_mapping_and_envelope():
    record = {"transaction_id": "T1", "bag_barcode": "B1", "operator": "x"}
    assert apply_field_mapping(record, {"transaction_id": "txnId", "operator": ""}) == {"txnId": "T1", "bag_barcode": "B1"}
    cfg = MesConfig(payload_mode="ENVELOPE", records_key="items", field_mapping_json=json.dumps({"bag_barcode": "barcode"}))
    body = build_request_body([record], cfg, "batch-1", "IFM01")
    assert body["batch_id"] == "batch-1" and body["items"][0]["barcode"] == "B1"
    assert isinstance(build_request_body([record], MesConfig(payload_mode="ARRAY"), "b", "s"), list)
