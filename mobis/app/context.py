"""Composition root: builds and wires every service (no widgets - usable in tests)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mobis import __version__
from mobis.core.inventory_manager import InventoryManager
from mobis.core.mapping_service import MappingService
from mobis.core.models import Severity
from mobis.core.transaction_manager import TransactionManager
from mobis.database.database import Database
from mobis.database.migrations import MigrationRunner
from mobis.database.repositories import Repositories
from mobis.database.seed import seed_initial_data
from mobis.hardware.barcode_scanner import ScannerManager
from mobis.hardware.plc_manager import PlcManager
from mobis.integrations.mes_controller import MesSyncController
from mobis.services.audit_service import AuditService, EventService
from mobis.services.auth_service import AuthService
from mobis.services.backup_service import BackupService
from mobis.services.configuration_service import ConfigurationService
from mobis.services.paths import AppPaths
from mobis.services.signal_hub import SignalHub
from mobis.utils import timeutil

log = logging.getLogger("mobis.app")


@dataclass
class AppContext:
    paths: AppPaths
    db: Database
    repos: Repositories
    hub: SignalHub
    config: ConfigurationService
    audit: AuditService
    events: EventService
    auth: AuthService
    inventory: InventoryManager
    mapping: MappingService
    plc: PlcManager
    scanner: ScannerManager
    mes: MesSyncController
    transactions: TransactionManager
    backup: BackupService
    simulate: bool = False
    schema_version: int = 0
    startup_messages: list[str] = field(default_factory=list)
    started: bool = False

    # ------------------------------------------------------------------ wiring helpers
    def refresh_equipment_signals(self) -> None:
        try:
            signals = [
                (e.ready_signal_address, e.ready_signal_ok_value)
                for e in self.mapping.list_equipment(active_only=True)
                if e.ready_signal_address.strip()
            ]
        except Exception:  # noqa: BLE001
            log.exception("Could not load equipment signals")
            return
        self.plc.set_equipment_signals(signals)

    def _on_transaction_finished(self, _number: str, _status: str) -> None:
        self.hub.transactions_changed.emit()
        self.hub.inventory_changed.emit()
        self.mes.refresh_counts()
        self.mes.sync_now()

    def _on_config_changed(self, section: str) -> None:
        if section == "app":
            app = self.config.app()
            timeutil.set_display_formats(app.date_format, app.time_format)
        if section in ("app", "barcode"):
            self.transactions.refresh_configuration()

    # ------------------------------------------------------------------ lifecycle
    def start_services(self) -> None:
        if self.started:
            return
        app = self.config.app()
        timeutil.set_display_formats(app.date_format, app.time_format)
        for number in self.transactions.recover_interrupted():
            self.startup_messages.append(f"Transaction {number} was interrupted by the last shutdown.")
        self.refresh_equipment_signals()
        self.plc.start()
        self.scanner.start()
        self.mes.start()
        self.transactions.start()
        self.started = True
        self.events.log(Severity.INFO, "SYSTEM", "APP_STARTED",
                        f"MOBIS {__version__} started (schema v{self.schema_version}, "
                        f"simulation={'forced' if self.simulate else 'per configuration'})")

    def shutdown(self) -> None:
        if not self.started:
            self.db.dispose()
            return
        self.events.log(Severity.INFO, "SYSTEM", "APP_STOPPED", "Application stopped")
        self.transactions.stop()
        self.scanner.stop()
        self.mes.stop()
        self.plc.stop()
        self.started = False
        self.db.dispose()


def build_context(paths: AppPaths, simulate: bool = False, demo_data: bool = True) -> AppContext:
    paths.ensure()
    db = Database(paths.db_file)
    backup = BackupService(paths)

    def _pre_migration_backup(current: int, target: int) -> None:
        location = backup.create_backup(f"pre_migration_v{current}_to_v{target}")
        log.warning("Database backup before migration: %s", location)

    runner = MigrationRunner(db.engine, before_upgrade=_pre_migration_backup)
    fresh = runner.current_version() == 0
    applied = runner.upgrade()
    if fresh:
        seed_initial_data(db, include_demo_bag_types=demo_data)

    repos = Repositories()
    hub = SignalHub()
    audit = AuditService(db, repos)
    config = ConfigurationService(db, repos, audit)
    events = EventService(db, repos)
    auth = AuthService(db, repos, audit, config)
    audit.set_user_provider(auth.audit_username)
    inventory = InventoryManager(db, repos, audit)
    mapping = MappingService(db, repos, config, audit, inventory)
    plc = PlcManager(config, events, force_simulation=simulate)
    scanner = ScannerManager(config, events, force_simulation=simulate)
    mes = MesSyncController(db, repos, config, events, hub)
    transactions = TransactionManager(
        db, repos, config, mapping, inventory, plc, events, audit, auth,
        paths.recovery_dir, mes.enqueue_callable(),
    )
    ctx = AppContext(
        paths=paths, db=db, repos=repos, hub=hub, config=config, audit=audit, events=events, auth=auth,
        inventory=inventory, mapping=mapping, plc=plc, scanner=scanner, mes=mes, transactions=transactions,
        backup=backup, simulate=simulate, schema_version=runner.current_version(),
    )
    if fresh:
        ctx.startup_messages.append("A new database was created with the initial equipment configuration.")
    elif applied:
        ctx.startup_messages.append(f"Database upgraded to schema version {applied[-1]}.")

    hub.equipment_changed.connect(ctx.refresh_equipment_signals)
    hub.equipment_changed.connect(transactions.refresh_configuration)
    hub.bag_types_changed.connect(transactions.refresh_configuration)
    config.changed.connect(ctx._on_config_changed)
    transactions.transaction_finished.connect(ctx._on_transaction_finished)
    return ctx
