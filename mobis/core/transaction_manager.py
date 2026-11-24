"""Material loading transaction orchestration.

Runs in the GUI thread and coordinates the validation engine, the state
machine, the database, the PLC gateway and the inventory.  Blocking PLC I/O
never happens here: PLC requests are asynchronous (results arrive through
callbacks / signals from the PLC worker thread).

Safety rules implemented here:

* Loading is authorised only after *every* planned stage is verified OK.
* Any NG immediately raises the PLC alarm and blocks further scanning until
  the alarm is reset (operator and/or PLC acknowledgement, configurable).
* No authorisation while the PLC link / heartbeat is down, the database is
  unavailable, an alarm is active or a destination limit switch is not OK.
* Every scan is recorded *before* the process advances; if the database
  cannot be written the process does not advance.
* The tank quantity is increased only when loading completion is confirmed,
  atomically with the transaction record and the MES queue entry.
* The PLC remains responsible for the physical interlock - the PC only sends
  validation results and alarm requests.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Protocol

from PySide6.QtCore import QObject, QTimer, Signal

from mobis.core.barcode_parser import describe_rule
from mobis.core.loading_monitor import LoadingEvent, LoadingEventType, LoadingMonitor
from mobis.core.models import (
    BagType,
    CheckItem,
    CheckOutcome,
    CheckResult,
    Equipment,
    NgReason,
    ScanResult,
    Severity,
    Stage,
    TransactionStatus,
    ValidationStatus,
)
from mobis.core.process_state_machine import (
    LOADING_STATES,
    SCANNING_STATES,
    STAGE_STATE,
    ProcessState,
    ProcessStateMachine,
)
from mobis.core.validation_engine import ConfigSnapshot, ValidationEngine
from mobis.utils.timeutil import fmt_datetime, utc_now

log = logging.getLogger("mobis.transaction")
S = ProcessState


class PlcGateway(Protocol):
    """What the transaction manager needs from the PLC layer.

    The implementation is also expected to provide the Qt signals
    ``signals_updated(object)``, ``connection_changed(bool, str)`` and
    ``alarm_acknowledged()``.
    """

    def is_ready(self) -> tuple[bool, str]: ...
    def get_signal(self, address: str) -> Optional[int]: ...
    def signal_values(self) -> dict[str, Optional[int]]: ...
    def normalize_address(self, address: str) -> str: ...
    def set_alarm(self, active: bool, reason_code: int = 0) -> None: ...
    def authorize_loading(self, hopper_code: int, destination_code: int,
                          callback: Callable[[bool, str], None]) -> None: ...
    def clear_authorization(self) -> None: ...


NG_TITLES = {
    NgReason.BAG_MISMATCH: "WRONG BAG",
    NgReason.COLOUR_MISMATCH: "COLOUR MISMATCH",
    NgReason.HOPPER_MISMATCH: "WRONG HOPPER",
    NgReason.SILO_MISMATCH: "WRONG SILO",
    NgReason.DISTRIBUTION_MISMATCH: "WRONG DISTRIBUTION TANK",
    NgReason.MOLD_MISMATCH: "WRONG MOLD / MACHINE",
    NgReason.DUPLICATE_BAG: "DUPLICATE BAG",
    NgReason.UNKNOWN_BARCODE: "UNKNOWN BARCODE",
    NgReason.CONFIGURATION_ERROR: "CONFIGURATION ERROR",
    NgReason.BARCODE_FORMAT: "UNREADABLE BAG BARCODE",
}

RESULT_FIELDS = {
    CheckItem.BAG: "bag_result",
    CheckItem.COLOUR: "colour_result",
    CheckItem.HOPPER: "hopper_result",
    CheckItem.SILO: "silo_result",
    CheckItem.DISTRIBUTION_TANK: "distribution_result",
    CheckItem.MOLD: "mold_result",
}

RESET_INSTRUCTIONS = {
    "OPERATOR": "press RESET ALARM",
    "PLC_ACK": "acknowledge the alarm on the PLC panel",
    "OPERATOR_OR_PLC": "press RESET ALARM (or acknowledge on the PLC panel)",
}


@dataclass
class StageRow:
    item: CheckItem
    expected: str
    scanned: str = ""
    result: CheckResult = CheckResult.PENDING
    message: str = ""


@dataclass(frozen=True)
class ReadinessCheck:
    label: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Banner:
    level: str  # neutral | info | ok | ng | warn
    title: str
    message: str = ""


def _idle_banner() -> Banner:
    return Banner("neutral", "SELECT BAG TYPE", "Select the bag type to start a material loading transaction.")


@dataclass
class ProcessContext:
    state: ProcessState = S.IDLE
    bag_type: Optional[BagType] = None
    plan: list[Stage] = field(default_factory=list)
    next_stage: Optional[Stage] = None
    rows: dict[CheckItem, StageRow] = field(default_factory=dict)
    overall: CheckResult = CheckResult.PENDING
    transaction_id: Optional[int] = None
    transaction_no: str = ""
    bag_barcode: str = ""
    bag_weight_kg: float = 0.0
    hopper: Optional[Equipment] = None
    silo: Optional[Equipment] = None
    distribution_tank: Optional[Equipment] = None
    mold: Optional[Equipment] = None
    destination: Optional[Equipment] = None
    existing_qty_kg: Optional[float] = None
    total_qty_kg: Optional[float] = None
    alarm_active: bool = False
    alarm_message: str = ""
    ng_count: int = 0
    banner: Banner = field(default_factory=_idle_banner)
    readiness: list[ReadinessCheck] = field(default_factory=list)
    loading_info: str = ""
    confirmation_mode: str = "OPERATOR"
    last_scan: str = ""
    last_completed: Optional[dict] = None


class PersistenceError(RuntimeError):
    pass


class TransactionManager(QObject):
    context_changed = Signal()
    notice = Signal(str, str)               # level, message (transient feedback)
    transaction_finished = Signal(str, str)  # transaction no, final status

    def __init__(self, db, repos, config, mapping, inventory, plc: PlcGateway, events, audit, auth,
                 recovery_dir: Path, mes_enqueue: Optional[Callable] = None):
        super().__init__()
        self._db = db
        self._repos = repos
        self._config = config
        self._mapping = mapping
        self._inventory = inventory
        self._plc = plc
        self._events = events
        self._audit = audit
        self._auth = auth
        self._recovery_dir = Path(recovery_dir)
        self._mes_enqueue = mes_enqueue
        self.sm = ProcessStateMachine()
        self.ctx = ProcessContext()
        self._snapshot: Optional[ConfigSnapshot] = None
        self._engine: Optional[ValidationEngine] = None
        self._monitor: Optional[LoadingMonitor] = None
        self._authorizing = False
        self._auth_retry_at = 0.0
        self._validation_passed = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._periodic)
        for name, slot in (
            ("signals_updated", self._on_plc_signals),
            ("connection_changed", self._on_plc_connection),
            ("alarm_acknowledged", self._on_plc_alarm_ack),
        ):
            signal = getattr(plc, name, None)
            if signal is not None:
                signal.connect(slot)

    # ================================================================== lifecycle
    def start(self) -> None:
        self.ctx.confirmation_mode = self._config.plc().loading_confirmation_mode
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def recover_interrupted(self) -> list[str]:
        """Close transactions left open by an unexpected stop (called at start-up)."""
        try:
            with self._db.session() as s:
                numbers = self._repos.transactions.mark_interrupted(s)
        except Exception:  # noqa: BLE001
            log.exception("Could not recover interrupted transactions")
            return []
        for number in numbers:
            self._events.log(Severity.WARNING, "TRANSACTION", "INTERRUPTED",
                             f"Transaction {number} was interrupted by an application stop; "
                             "quantity not updated - verify the physical state.")
        return numbers

    # ================================================================== queries
    @property
    def state(self) -> ProcessState:
        return self.sm.state

    @property
    def is_transaction_open(self) -> bool:
        return self.ctx.transaction_id is not None

    def bag_type_options(self) -> list[BagType]:
        return self._mapping.list_bag_types(active_only=True)

    # ================================================================== operator actions
    def select_bag_type(self, bag_type_id: int) -> tuple[bool, str]:
        if self.is_transaction_open and self.sm.state not in (S.LOADING_COMPLETED, S.CANCELLED):
            return False, "A transaction is in progress. Cancel it before changing the bag type."
        try:
            snapshot = self._mapping.load_snapshot()
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not load configuration snapshot")
            self.ctx.banner = Banner("ng", "DATABASE ERROR", f"Configuration could not be loaded: {exc}")
            self._emit()
            return False, str(exc)
        bag_type = snapshot.bag_types.get(bag_type_id)
        if bag_type is None or not bag_type.is_active:
            return False, "Bag type not found or inactive."
        engine = ValidationEngine(snapshot)
        errors = engine.check_bag_type(bag_type)
        if errors:
            self._go_idle()
            self.ctx.banner = Banner(
                "ng", "CONFIGURATION ERROR",
                f"Bag type '{bag_type.name}' cannot be used:\n" + "\n".join(f"• {e}" for e in errors),
            )
            self._events.log(Severity.WARNING, "VALIDATION", "BAG_TYPE_CONFIG_ERROR",
                             f"Bag type {bag_type.code} rejected: {' | '.join(errors)}")
            self._emit()
            return False, "\n".join(errors)

        plan = engine.stage_plan(bag_type)
        if not self.sm.can_transition(S.BAG_TYPE_SELECTED):
            return False, f"Bag type cannot be changed in state {self.sm.state.label}."
        self.sm.select_bag_type(plan)
        self._snapshot, self._engine = snapshot, engine
        self._init_context(bag_type, plan)
        self._emit()
        return True, ""

    def refresh_configuration(self) -> None:
        """Reload mappings when no transaction is open (keeps an open one consistent)."""
        if self.is_transaction_open:
            return
        if self.ctx.bag_type is not None and self.sm.state in (S.BAG_TYPE_SELECTED, S.LOADING_COMPLETED, S.CANCELLED):
            ok, _msg = self.select_bag_type(self.ctx.bag_type.id)
            if not ok and self.sm.state != S.IDLE:
                self._go_idle()
                self._emit()

    def new_transaction(self) -> tuple[bool, str]:
        if self.is_transaction_open and self.sm.state not in (S.LOADING_COMPLETED, S.CANCELLED):
            return False, "Cancel the current transaction first."
        if self.ctx.bag_type is not None:
            return self.select_bag_type(self.ctx.bag_type.id)
        self._go_idle()
        self._emit()
        return True, ""

    def process_scan(self, barcode: str, source: str) -> None:
        barcode = (barcode or "").strip()
        if not barcode:
            return
        self.ctx.last_scan = barcode
        state = self.sm.state

        if self.ctx.alarm_active:
            self._record_ignored(barcode, source, "Alarm active - scan ignored")
            self.notice.emit("ng", "Alarm active: reset the alarm before scanning again.")
            self._emit()
            return
        if state == S.IDLE:
            self._record_ignored(barcode, source, "No bag type selected")
            self.notice.emit("warn", "Select the bag type before scanning.")
            self._emit()
            return
        if state in (S.LOADING_COMPLETED, S.CANCELLED):
            if self.ctx.bag_type is None or not self._config.app().auto_new_transaction:
                self._record_ignored(barcode, source, "No open transaction")
                self.notice.emit("warn", "Press NEW TRANSACTION to start the next loading.")
                self._emit()
                return
            ok, message = self.select_bag_type(self.ctx.bag_type.id)
            if not ok:
                self.notice.emit("ng", message)
                return
            state = self.sm.state
        if state in (S.VALIDATION_PASSED, *LOADING_STATES):
            self._record_ignored(barcode, source, f"Scan ignored in state {state.value}")
            self.notice.emit("warn", "Loading in progress - scan ignored.")
            self._emit()
            return
        if state not in SCANNING_STATES:
            self._record_ignored(barcode, source, f"Scan ignored in state {state.value}")
            self._emit()
            return
        stage = self.sm.next_stage
        if stage is None:
            return
        try:
            if stage == Stage.BAG:
                self._handle_bag_scan(barcode, source)
            else:
                self._handle_equipment_scan(stage, barcode, source)
        except PersistenceError as exc:
            self.ctx.banner = Banner(
                "ng", "DATABASE ERROR",
                f"The scan could not be recorded ({exc}). Loading is blocked until the database is available.",
            )
            self._events.log(Severity.CRITICAL, "DATABASE", "WRITE_FAILED", f"Scan not recorded: {exc}",
                             transaction_id=self.ctx.transaction_id)
        self._emit()

    def reset_alarm(self, source: str = "OPERATOR") -> tuple[bool, str]:
        mode = self._config.plc().alarm_reset_mode
        if not self.ctx.alarm_active:
            self._plc.set_alarm(False)
            return True, "No active alarm."
        if source == "OPERATOR" and mode == "PLC_ACK":
            return False, "This alarm must be acknowledged on the PLC panel."
        if source == "PLC" and mode == "OPERATOR":
            return False, "PLC acknowledgement is not enabled for alarm reset."
        self._plc.set_alarm(False)
        self.ctx.alarm_active = False
        self.ctx.alarm_message = ""
        who = "PLC acknowledgement" if source == "PLC" else f"operator ({self._auth.audit_username()})"
        self._events.log(Severity.INFO, "ALARM", "ALARM_RESET", f"Alarm reset by {who}",
                         transaction_id=self.ctx.transaction_id)
        if self.sm.state == S.VALIDATION_FAILED:
            self.sm.resume_after_failure()
            self._persist_state()
            stage = self.sm.next_stage
            if stage is None:
                self._on_all_verified()
            else:
                self.ctx.banner = Banner(
                    "info", f"RESCAN {stage.label.upper()}",
                    f"Alarm reset. Scan the correct {stage.label.lower()} - expected: "
                    f"{self._expected_label(stage)}. Or cancel the transaction.",
                )
        else:
            self.ctx.banner = Banner("neutral", "ALARM RESET", self.ctx.banner.message)
        self._emit()
        return True, ""

    def cancel(self, reason: str = "Cancelled by operator") -> tuple[bool, str]:
        state = self.sm.state
        if not self.is_transaction_open:
            if state in (S.BAG_TYPE_SELECTED, S.LOADING_COMPLETED, S.CANCELLED):
                self._go_idle()
                self._emit()
                return True, ""
            return False, "There is no transaction to cancel."

        authorised = state in LOADING_STATES or (state == S.VALIDATION_PASSED and self._authorizing)
        if state in LOADING_STATES or state == S.VALIDATION_PASSED:
            self._plc.clear_authorization()
        status = (
            TransactionStatus.REJECTED
            if self.ctx.ng_count > 0 and not self._validation_passed
            else TransactionStatus.CANCELLED
        )
        note = reason
        if authorised:
            note += (" | Cancelled after loading authorisation: verify the physical state of the hopper; "
                     "tank quantity NOT updated.")
        now = utc_now()
        tx_no = self.ctx.transaction_no
        try:
            with self._db.session() as s:
                tx = self._repos.transactions.update(
                    s, self.ctx.transaction_id, status=status.value, process_state=S.CANCELLED.value,
                    ended_at=now, notes=note,
                )
                if self._mes_enqueue is not None:
                    self._mes_enqueue(s, tx)
        except Exception:  # noqa: BLE001
            log.exception("Cancellation of %s could not be stored (will be marked INTERRUPTED at restart)", tx_no)
        self.sm.cancel(reason)
        self._monitor = None
        self._authorizing = False
        self.ctx.transaction_id = None
        self._events.log(Severity.WARNING if authorised else Severity.INFO, "TRANSACTION",
                         f"TRANSACTION_{status.value}", f"{tx_no}: {note}")
        alarm_note = " The alarm is still active - reset it before scanning." if self.ctx.alarm_active else ""
        self.ctx.banner = Banner(
            "neutral", "TRANSACTION CANCELLED" if status == TransactionStatus.CANCELLED else "TRANSACTION REJECTED",
            f"{tx_no} closed as {status.value}.{alarm_note} Scan the next bag or select another bag type.",
        )
        self.transaction_finished.emit(tx_no, status.value)
        self._emit()
        return True, ""

    def confirm_loading_complete(self, forced_by: str = "") -> tuple[bool, str]:
        if self.sm.state not in LOADING_STATES:
            return False, "No loading is in progress."
        mode = self._monitor.mode if self._monitor else "OPERATOR"
        if mode != "OPERATOR" and not forced_by:
            return False, "In this configuration the PLC confirms loading completion."
        if forced_by:
            self._audit.log("TRANSACTION", "FORCE_COMPLETE",
                            f"Loading of {self.ctx.transaction_no} force-completed by {forced_by}",
                            entity="material_transaction", entity_id=self.ctx.transaction_id, username=forced_by)
        self._complete_loading(f"FORCED:{forced_by}" if forced_by else "OPERATOR")
        return True, ""

    def retry_authorization(self) -> None:
        self._auth_retry_at = 0.0
        self._try_authorize(force_emit=True)

    # ================================================================== scanning
    def _handle_bag_scan(self, barcode: str, source: str) -> None:
        bag_type = self.ctx.bag_type
        prevent = self._snapshot.barcode_format.prevent_duplicate_bag
        check = self._engine.validate_bag(bag_type, barcode, self._used_bag_lookup if prevent else None)
        failure = check.failure
        target = S.BAG_SCANNED if check.ok else S.VALIDATION_FAILED
        updates = {
            "bag_result": check.bag.result.value,
            "colour_result": check.colour.result.value,
            "process_state": target.value,
        }
        if check.ok:
            updates.update(bag_barcode=barcode, bag_lot=check.parsed.lot, bag_serial=check.parsed.serial,
                           bag_weight_kg=check.weight_kg)
        else:
            updates.update(ng_count=self.ctx.ng_count + 1, validation_status=ValidationStatus.NG.value,
                           last_error=failure.message)
        self._persist_scan(Stage.BAG, barcode, source, check.ok, check.bag.expected,
                           failure.message if failure else check.bag.message, None, updates)

        self._update_row(check.bag)
        self._update_row(check.colour)
        if check.ok:
            self.ctx.bag_barcode = barcode
            self.ctx.bag_weight_kg = check.weight_kg
            self._update_quantities()
            self.sm.stage_verified(Stage.BAG)
            self._prompt_next_stage()
        else:
            self._fail(Stage.BAG, failure)

    def _handle_equipment_scan(self, stage: Stage, barcode: str, source: str) -> None:
        outcome = self._engine.validate_equipment(stage, self.ctx.bag_type, barcode)
        target = STAGE_STATE[stage] if outcome.ok else S.VALIDATION_FAILED
        updates = {RESULT_FIELDS[stage.check_item]: outcome.result.value, "process_state": target.value}
        if outcome.ok and stage == Stage.MOLD and outcome.equipment is not None:
            updates.update(mold_id=outcome.equipment.id, mold_code=outcome.equipment.code)
        if not outcome.ok:
            updates.update(ng_count=self.ctx.ng_count + 1, validation_status=ValidationStatus.NG.value,
                           last_error=outcome.message)
        self._persist_scan(stage, barcode, source, outcome.ok, outcome.expected, outcome.message,
                           outcome.equipment.id if outcome.equipment else None, updates)

        self._update_row(outcome)
        if not outcome.ok:
            self._fail(stage, outcome)
            return
        if stage == Stage.MOLD:
            self.ctx.mold = outcome.equipment
        self.sm.stage_verified(stage)
        if self.sm.next_stage is None:
            self._on_all_verified()
        else:
            self._prompt_next_stage()

    def _fail(self, stage: Stage, outcome: CheckOutcome) -> None:
        self.ctx.ng_count += 1
        self.sm.stage_failed(stage)
        self.ctx.alarm_active = True
        self.ctx.alarm_message = outcome.message
        self._plc.set_alarm(True, int(outcome.ng_reason))
        how = RESET_INSTRUCTIONS.get(self._config.plc().alarm_reset_mode, RESET_INSTRUCTIONS["OPERATOR"])
        title = NG_TITLES.get(outcome.ng_reason, f"{outcome.item.label.upper()} MISMATCH")
        self.ctx.banner = Banner(
            "ng", f"NG – {title}",
            f"{outcome.message}\nTo continue: {how}, then rescan the correct {stage.label.lower()} "
            "or cancel the transaction.",
        )
        self._events.log(Severity.WARNING, "VALIDATION", "SCAN_NG",
                         f"{self.ctx.transaction_no} [{stage.value}] {outcome.message}",
                         transaction_id=self.ctx.transaction_id)

    def _on_all_verified(self) -> None:
        results = {item: row.result for item, row in self.ctx.rows.items()}
        overall = ValidationEngine.overall(results)
        self.ctx.overall = overall
        if overall != CheckResult.OK:
            log.error("All stages verified but logic matrix is %s: %s", overall, results)
            self.ctx.banner = Banner("ng", "NG – VALIDATION INCOMPLETE",
                                     "Not every required check is OK. Cancel and restart the transaction.")
            return
        try:
            with self._db.session() as s:
                self._repos.transactions.update(
                    s, self.ctx.transaction_id, validation_status=ValidationStatus.OK.value,
                    validated_at=utc_now(), process_state=S.VALIDATION_PASSED.value, last_error="",
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not store validation result")
            self.ctx.banner = Banner("ng", "DATABASE ERROR",
                                     f"Validation result could not be recorded ({exc}). Loading is blocked.")
            return
        self.sm.validation_passed()
        self._validation_passed = True
        self._events.log(Severity.INFO, "VALIDATION", "VALIDATION_OK",
                         f"{self.ctx.transaction_no}: all checks OK", transaction_id=self.ctx.transaction_id)
        self._try_authorize(force_emit=True)

    # ================================================================== authorisation
    def readiness_checks(self) -> list[ReadinessCheck]:
        checks: list[ReadinessCheck] = []
        db_ok, db_detail = self._db.health_check()
        checks.append(ReadinessCheck("Database", db_ok, "OK" if db_ok else db_detail))
        plc_ok, plc_detail = self._plc.is_ready()
        checks.append(ReadinessCheck("PLC communication", plc_ok, plc_detail))
        checks.append(ReadinessCheck("Alarm", not self.ctx.alarm_active,
                                     "Alarm active" if self.ctx.alarm_active else "No alarm"))
        seen = set()
        for eq in (self.ctx.hopper, self.ctx.silo, self.ctx.distribution_tank, self.ctx.mold):
            if eq is None or eq.id in seen or not eq.ready_signal_address.strip():
                continue
            seen.add(eq.id)
            value = self._plc.get_signal(eq.ready_signal_address)
            label = f"{eq.name} limit switch"
            if value is None:
                checks.append(ReadinessCheck(label, False, "signal unavailable"))
            else:
                ok = value == (eq.ready_signal_ok_value & 0xFFFF)
                checks.append(ReadinessCheck(label, ok, "OK" if ok else f"NOT OK (value {value})"))
        cfg = self._config.plc()
        if cfg.loading_confirmation_mode == "PLC_SIGNAL" and not cfg.loading_complete_address.strip():
            checks.append(ReadinessCheck("Loading confirmation", False, "loading-complete address not configured"))
        if cfg.loading_confirmation_mode == "SHUTTER_CYCLE" and not cfg.shutter_status_address.strip():
            checks.append(ReadinessCheck("Loading confirmation", False, "shutter status address not configured"))
        return checks

    def _try_authorize(self, force_emit: bool = False) -> None:
        if self.sm.state != S.VALIDATION_PASSED or self._authorizing:
            return
        if time.monotonic() < self._auth_retry_at:
            return
        before = (self.ctx.banner, tuple(self.ctx.readiness))
        checks = self.readiness_checks()
        self.ctx.readiness = checks
        failed = [c for c in checks if not c.ok]
        if failed:
            self.ctx.banner = Banner(
                "warn", "VALIDATION OK – WAITING",
                "Loading cannot be authorised yet:\n" + "\n".join(f"• {c.label}: {c.detail}" for c in failed),
            )
            if force_emit or before != (self.ctx.banner, tuple(self.ctx.readiness)):
                self._emit()
            return
        self._authorizing = True
        hopper_code = (self.ctx.hopper.plc_code if self.ctx.hopper else 0) or 0
        destination_code = (self.ctx.destination.plc_code if self.ctx.destination else 0) or 0
        self.ctx.banner = Banner("ok", "VALIDATION OK", "Sending loading authorisation to the PLC…")
        self._emit()
        self._plc.authorize_loading(hopper_code, destination_code, self._on_authorize_result)

    def _on_authorize_result(self, ok: bool, error: str) -> None:
        self._authorizing = False
        if self.sm.state != S.VALIDATION_PASSED:
            if ok:  # transaction was cancelled while the request was in flight
                self._plc.clear_authorization()
            return
        if not ok:
            self._auth_retry_at = time.monotonic() + 3.0
            self.ctx.banner = Banner("warn", "VALIDATION OK – WAITING",
                                     f"PLC authorisation failed: {error}. Retrying automatically…")
            self._events.log(Severity.ERROR, "PLC", "AUTHORIZATION_FAILED", f"{self.ctx.transaction_no}: {error}",
                             transaction_id=self.ctx.transaction_id)
            self._emit()
            return
        try:
            with self._db.session() as s:
                self._repos.transactions.update(s, self.ctx.transaction_id, authorized_at=utc_now(),
                                                process_state=S.LOADING_AUTHORIZED.value)
        except Exception as exc:  # noqa: BLE001
            log.exception("Authorisation could not be recorded; revoking")
            self._plc.clear_authorization()
            self.ctx.banner = Banner("ng", "DATABASE ERROR",
                                     f"Authorisation could not be recorded ({exc}); it was revoked.")
            self._emit()
            return
        self.sm.loading_authorized()
        cfg = self._config.plc()
        self._monitor = LoadingMonitor(cfg, normalize=self._plc.normalize_address)
        self._monitor.start(time.monotonic())
        self.ctx.confirmation_mode = self._monitor.mode
        self.ctx.loading_info = ""
        hopper = self.ctx.hopper.name if self.ctx.hopper else "the hopper"
        instructions = {
            "OPERATOR": f"Fill the material into {hopper}, then press LOADING COMPLETE.",
            "PLC_SIGNAL": f"Fill the material into {hopper}. The PLC confirms completion automatically.",
            "SHUTTER_CYCLE": f"Fill the material into {hopper}. Completion is confirmed when the shutter closes.",
        }[self._monitor.mode]
        self.ctx.banner = Banner("ok", "OK – LOADING AUTHORISED", f"{hopper} shutter released by the PLC. {instructions}")
        self._events.log(Severity.INFO, "TRANSACTION", "LOADING_AUTHORIZED",
                         f"{self.ctx.transaction_no}: loading authorised for {hopper}",
                         transaction_id=self.ctx.transaction_id)
        self._emit()

    # ================================================================== loading
    def _handle_loading_events(self, events: list[LoadingEvent]) -> None:
        if not events:
            return
        for event in events:
            if event.type == LoadingEventType.STARTED and self.sm.state == S.LOADING_AUTHORIZED:
                self.sm.loading_started()
                self._persist_state()
                self.ctx.banner = Banner("ok", "LOADING IN PROGRESS", self.ctx.banner.message)
                self.ctx.loading_info = event.message
            elif event.type == LoadingEventType.COMPLETED:
                self._complete_loading(self._monitor.mode if self._monitor else "PLC")
                return
            elif event.type == LoadingEventType.MATERIAL_DETECTED:
                self.ctx.loading_info = event.message
            else:
                self.ctx.loading_info = event.message
                if event.type == LoadingEventType.TIMEOUT:
                    self.ctx.banner = Banner("warn", "LOADING NOT CONFIRMED", event.message)
                self._events.log(Severity.WARNING, "TRANSACTION", event.type.value,
                                 f"{self.ctx.transaction_no}: {event.message}", transaction_id=self.ctx.transaction_id)
        self._emit()

    def _complete_loading(self, source: str) -> None:
        ctx = self.ctx
        destination = ctx.destination
        now = utc_now()
        username = self._auth.record_username()
        before = after = None
        try:
            with self._db.session() as s:
                if destination is not None:
                    before, after = self._inventory.apply_loading(
                        s, destination.id, ctx.bag_weight_kg, ctx.transaction_id, username or "station"
                    )
                tx = self._repos.transactions.update(
                    s, ctx.transaction_id,
                    status=TransactionStatus.COMPLETED.value,
                    process_state=S.LOADING_COMPLETED.value,
                    validation_status=ValidationStatus.OK.value,
                    completed_at=now, ended_at=now,
                    quantity_before_kg=before, quantity_after_kg=after,
                    confirmation_source=source,
                )
                if self._mes_enqueue is not None:
                    self._mes_enqueue(s, tx)
        except Exception as exc:  # noqa: BLE001
            log.exception("Completion of %s could not be stored", ctx.transaction_no)
            path = self._write_recovery_file(source, str(exc))
            ctx.banner = Banner(
                "ng", "DATABASE ERROR – NOT SAVED",
                f"Loading of {ctx.transaction_no} could not be saved ({exc}). Do not start another loading; "
                f"contact the supervisor. Recovery data: {path}",
            )
            self._events.log(Severity.CRITICAL, "DATABASE", "COMPLETION_NOT_SAVED",
                             f"{ctx.transaction_no}: {exc}. Recovery file: {path}")
            self._emit()
            return

        self.sm.loading_completed()
        self._plc.clear_authorization()
        self._monitor = None
        ctx.loading_info = ""
        tx_no = ctx.transaction_no
        ctx.transaction_id = None
        ctx.existing_qty_kg = before
        ctx.total_qty_kg = after
        dest_text = f"{destination.name}: {after:,.1f} kg" if destination is not None and after is not None else ""
        ctx.last_completed = {
            "transaction_no": tx_no,
            "completed_at": now,
            "bag_type": ctx.bag_type.name if ctx.bag_type else "",
            "weight_kg": ctx.bag_weight_kg,
            "destination": destination.name if destination else "",
            "quantity_after_kg": after,
        }
        ctx.banner = Banner(
            "ok", "OK – LOADING COMPLETE",
            f"{tx_no}: {ctx.bag_weight_kg:g} kg {ctx.bag_type.name if ctx.bag_type else ''} loaded."
            + (f" New quantity {dest_text}." if dest_text else "")
            + " Scan the next bag to continue.",
        )
        self._events.log(Severity.INFO, "TRANSACTION", "LOADING_COMPLETED",
                         f"{tx_no}: completed ({source})" + (f", {dest_text}" if dest_text else ""))
        self.transaction_finished.emit(tx_no, TransactionStatus.COMPLETED.value)
        self._emit()

    # ================================================================== PLC callbacks
    def _on_plc_signals(self, values) -> None:
        state = self.sm.state
        if state in LOADING_STATES and self._monitor is not None:
            self._handle_loading_events(self._monitor.update(values, time.monotonic()))
        elif state == S.VALIDATION_PASSED:
            self._try_authorize()

    def _on_plc_connection(self, connected: bool, message: str) -> None:
        state = self.sm.state
        if state == S.VALIDATION_PASSED:
            self._try_authorize(force_emit=True)
        elif state in LOADING_STATES and not connected:
            self.ctx.loading_info = ("PLC connection lost during loading - waiting for reconnection. "
                                     "The PLC interlock remains in control.")
            self._events.log(Severity.WARNING, "PLC", "CONNECTION_LOST_DURING_LOADING",
                             f"{self.ctx.transaction_no}: {message}", transaction_id=self.ctx.transaction_id)
            self._emit()

    def _on_plc_alarm_ack(self) -> None:
        if self.ctx.alarm_active and self._config.plc().alarm_reset_mode in ("PLC_ACK", "OPERATOR_OR_PLC"):
            self.reset_alarm("PLC")

    def _periodic(self) -> None:
        state = self.sm.state
        if state == S.VALIDATION_PASSED:
            self._try_authorize()
        elif state in LOADING_STATES and self._monitor is not None:
            self._handle_loading_events(self._monitor.update(self._plc.signal_values(), time.monotonic()))
        elif state in SCANNING_STATES and self.is_transaction_open and self.sm.next_stage is None:
            self._on_all_verified()
            self._emit()

    # ================================================================== helpers
    def _init_context(self, bag_type: BagType, plan: list[Stage]) -> None:
        engine = self._engine
        previous = self.ctx
        ctx = ProcessContext()
        ctx.bag_type = bag_type
        ctx.plan = plan
        ctx.hopper = engine.expected_equipment(bag_type, Stage.HOPPER)
        ctx.silo = engine.expected_equipment(bag_type, Stage.SILO)
        ctx.distribution_tank = engine.expected_equipment(bag_type, Stage.DISTRIBUTION_TANK)
        ctx.mold = engine.expected_equipment(bag_type, Stage.MOLD)
        ctx.destination = engine.destination(bag_type)
        ctx.bag_weight_kg = bag_type.weight_kg
        results = engine.initial_results(bag_type)
        ctx.rows = {item: StageRow(item, self._expected_text(item, bag_type, ctx), result=results[item])
                    for item in CheckItem}
        ctx.alarm_active = previous.alarm_active
        ctx.alarm_message = previous.alarm_message
        ctx.last_completed = previous.last_completed
        ctx.confirmation_mode = self._config.plc().loading_confirmation_mode
        ctx.banner = Banner("info", "SCAN BAG BARCODE", f"Bag type {bag_type.name} selected. Scan the bag barcode.")
        if previous.alarm_active:
            ctx.banner = Banner("ng", "ALARM ACTIVE", "Reset the alarm before scanning.")
        self.ctx = ctx
        self._validation_passed = False
        self._monitor = None
        self._update_quantities()

    def _expected_text(self, item: CheckItem, bag_type: BagType, ctx: ProcessContext) -> str:
        plan = ctx.plan
        if item == CheckItem.BAG:
            return f"{bag_type.code}: {describe_rule(bag_type)}"
        if item == CheckItem.COLOUR:
            return bag_type.colour + (f" ({bag_type.colour_code})" if bag_type.colour_code else "")
        if item == CheckItem.HOPPER:
            return ctx.hopper.display if ctx.hopper else "Not configured"
        if item == CheckItem.SILO:
            if Stage.SILO in plan:
                return ctx.silo.display if ctx.silo else "Not configured"
            return f"Scan not required (routed to {ctx.silo.name})" if ctx.silo else "Not required"
        if item == CheckItem.DISTRIBUTION_TANK:
            if Stage.DISTRIBUTION_TANK in plan:
                return ctx.distribution_tank.display if ctx.distribution_tank else "Not configured"
            if ctx.distribution_tank:
                return f"Scan not required (routed to {ctx.distribution_tank.name})"
            return "Not required"
        if Stage.MOLD in plan:
            if ctx.mold:
                return ctx.mold.display
            return f"Any mold fed by {ctx.hopper.name}" if ctx.hopper else "Any linked mold"
        return "Not required"

    def _expected_label(self, stage: Stage) -> str:
        row = self.ctx.rows.get(stage.check_item)
        return row.expected if row else ""

    def _update_row(self, outcome: CheckOutcome) -> None:
        row = self.ctx.rows.get(outcome.item)
        if row is None:
            return
        row.scanned = outcome.scanned
        row.result = outcome.result
        row.message = outcome.message
        self.ctx.overall = ValidationEngine.overall({i: r.result for i, r in self.ctx.rows.items()})

    def _update_quantities(self) -> None:
        ctx = self.ctx
        if ctx.destination is None:
            ctx.existing_qty_kg = ctx.total_qty_kg = None
            return
        try:
            existing = self._inventory.quantity(ctx.destination.id)
        except Exception:  # noqa: BLE001
            log.exception("Could not read tank quantity")
            ctx.existing_qty_kg = ctx.total_qty_kg = None
            return
        ctx.existing_qty_kg = existing
        ctx.total_qty_kg = round(existing + ctx.bag_weight_kg, 3)

    def _prompt_next_stage(self) -> None:
        stage = self.sm.next_stage
        if stage is None:
            return
        self.ctx.banner = Banner("info", f"SCAN {stage.label.upper()} BARCODE",
                                 f"Expected: {self._expected_label(stage)}")

    def _go_idle(self) -> None:
        if self.sm.state != S.IDLE:
            if self.sm.can_transition(S.IDLE):
                self.sm.reset()
            else:
                self.sm.force_idle()
        previous = self.ctx
        self.ctx = ProcessContext(alarm_active=previous.alarm_active, alarm_message=previous.alarm_message,
                                  last_completed=previous.last_completed,
                                  confirmation_mode=self._config.plc().loading_confirmation_mode)
        self._snapshot = self._engine = None
        self._monitor = None
        self._validation_passed = False

    def _emit(self) -> None:
        self.ctx.state = self.sm.state
        self.ctx.next_stage = self.sm.next_stage
        self.context_changed.emit()

    def _create_transaction(self, s) -> tuple[int, str]:
        app = self._config.app()
        ctx = self.ctx
        bag_type = ctx.bag_type
        number = self._repos.transactions.next_transaction_no(s, app.station_code, datetime.now().strftime("%Y%m%d"))
        results = {item: row.result for item, row in ctx.rows.items()}
        specific_mold = ctx.mold if (Stage.MOLD in ctx.plan and bag_type.mold_id) else None
        tx = self._repos.transactions.create(
            s,
            transaction_no=number,
            transaction_uuid=str(uuid.uuid4()),
            station_code=app.station_code,
            status=TransactionStatus.IN_PROGRESS.value,
            process_state=self.sm.state.value,
            validation_status=ValidationStatus.PENDING.value,
            operator_username=self._auth.record_username(),
            bag_type_id=bag_type.id,
            bag_type_code=bag_type.code,
            bag_type_name=bag_type.name,
            material_type=bag_type.material_type,
            colour=bag_type.colour,
            bag_weight_kg=bag_type.weight_kg,
            hopper_id=ctx.hopper.id if ctx.hopper else None,
            hopper_code=ctx.hopper.code if ctx.hopper else "",
            silo_id=ctx.silo.id if ctx.silo else None,
            silo_code=ctx.silo.code if ctx.silo else "",
            distribution_tank_id=ctx.distribution_tank.id if ctx.distribution_tank else None,
            distribution_tank_code=ctx.distribution_tank.code if ctx.distribution_tank else "",
            mold_id=specific_mold.id if specific_mold else None,
            mold_code=specific_mold.code if specific_mold else "",
            destination_id=ctx.destination.id if ctx.destination else None,
            destination_code=ctx.destination.code if ctx.destination else "",
            silo_result=results[CheckItem.SILO].value,
            distribution_result=results[CheckItem.DISTRIBUTION_TANK].value,
            mold_result=results[CheckItem.MOLD].value,
        )
        return tx.id, number

    def _persist_scan(self, stage: Stage, barcode: str, source: str, ok: bool, expected: str, message: str,
                      equipment_id: Optional[int], updates: dict) -> None:
        """Record the scan and the transaction update atomically (raises PersistenceError)."""
        new_tx: Optional[tuple[int, str]] = None
        try:
            with self._db.session() as s:
                tx_id = self.ctx.transaction_id
                if tx_id is None:
                    new_tx = self._create_transaction(s)
                    tx_id = new_tx[0]
                self._repos.transactions.update(s, tx_id, **updates)
                self._repos.transactions.add_scan(
                    s, transaction_id=tx_id, stage=stage.value, barcode=barcode, input_source=source,
                    result=(ScanResult.OK if ok else ScanResult.NG).value, expected_value=expected[:500],
                    matched_equipment_id=equipment_id, message=message[:1000],
                    operator_username=self._auth.record_username(),
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("Scan persistence failed")
            raise PersistenceError(str(exc)) from exc
        if new_tx is not None:
            self.ctx.transaction_id, self.ctx.transaction_no = new_tx
            self._events.log(Severity.INFO, "TRANSACTION", "TRANSACTION_STARTED",
                             f"{new_tx[1]} started for bag type {self.ctx.bag_type.code}", transaction_id=new_tx[0])

    def _record_ignored(self, barcode: str, source: str, message: str) -> None:
        try:
            with self._db.session() as s:
                self._repos.transactions.add_scan(
                    s, transaction_id=self.ctx.transaction_id,
                    stage=self.sm.next_stage.value if self.sm.next_stage else "NONE",
                    barcode=barcode, input_source=source, result=ScanResult.IGNORED.value,
                    message=message, operator_username=self._auth.record_username(),
                )
        except Exception:  # noqa: BLE001
            log.exception("Could not record ignored scan")

    def _persist_state(self) -> None:
        if self.ctx.transaction_id is None:
            return
        try:
            with self._db.session() as s:
                self._repos.transactions.update(s, self.ctx.transaction_id, process_state=self.sm.state.value)
        except Exception:  # noqa: BLE001
            log.exception("Could not persist process state")

    def _used_bag_lookup(self, barcode: str) -> Optional[str]:
        with self._db.read_session() as s:
            row = self._repos.transactions.find_completed_by_bag_barcode(s, barcode)
            if row is None:
                return None
            return f"{row.transaction_no}, {fmt_datetime(row.completed_at)}"

    def _write_recovery_file(self, source: str, error: str) -> Path:
        ctx = self.ctx
        self._recovery_dir.mkdir(parents=True, exist_ok=True)
        path = self._recovery_dir / f"{ctx.transaction_no or 'unknown'}_{int(time.time())}.json"
        data = {
            "error": error,
            "written_at": fmt_datetime(utc_now()),
            "transaction_no": ctx.transaction_no,
            "transaction_id": ctx.transaction_id,
            "bag_type": ctx.bag_type.code if ctx.bag_type else "",
            "bag_barcode": ctx.bag_barcode,
            "bag_weight_kg": ctx.bag_weight_kg,
            "hopper": ctx.hopper.code if ctx.hopper else "",
            "destination": ctx.destination.code if ctx.destination else "",
            "confirmation_source": source,
            "operator": self._auth.record_username(),
        }
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            log.exception("Recovery file could not be written either")
        return path
