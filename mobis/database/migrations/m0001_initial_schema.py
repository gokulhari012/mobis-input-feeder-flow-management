"""Initial schema (version 1)."""

VERSION = 1
NAME = "initial_schema"

_CONFIG_TABLE = """
CREATE TABLE {name} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
)
"""

STATEMENTS = [
    # ------------------------------------------------------------------ equipment
    """
    CREATE TABLE equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        equipment_code TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        category TEXT NOT NULL CHECK (category IN ('HOPPER','SILO','DISTRIBUTION_TANK','MOLD')),
        barcode TEXT NOT NULL COLLATE NOCASE,
        description TEXT NOT NULL DEFAULT '',
        capacity_kg REAL,
        plc_code INTEGER,
        ready_signal_address TEXT NOT NULL DEFAULT '',
        ready_signal_ok_value INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (category, barcode)
    )
    """,
    """
    CREATE TABLE equipment_mappings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_equipment_id INTEGER NOT NULL REFERENCES equipment(id),
        target_equipment_id INTEGER NOT NULL REFERENCES equipment(id),
        is_active INTEGER NOT NULL DEFAULT 1,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source_equipment_id, target_equipment_id),
        CHECK (source_equipment_id <> target_equipment_id)
    )
    """,
    # ------------------------------------------------------------------ bag types
    """
    CREATE TABLE bag_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        material_type TEXT NOT NULL,
        colour TEXT NOT NULL,
        colour_code TEXT NOT NULL DEFAULT '',
        barcode_match_type TEXT NOT NULL DEFAULT 'PREFIX'
            CHECK (barcode_match_type IN ('EXACT','PREFIX','CONTAINS','REGEX','MATERIAL_CODE')),
        barcode_match_value TEXT NOT NULL DEFAULT '',
        weight_kg REAL NOT NULL CHECK (weight_kg > 0),
        hopper_id INTEGER NOT NULL REFERENCES equipment(id),
        silo_id INTEGER REFERENCES equipment(id),
        silo_scan_required INTEGER NOT NULL DEFAULT 0,
        distribution_tank_id INTEGER REFERENCES equipment(id),
        distribution_scan_required INTEGER NOT NULL DEFAULT 0,
        mold_id INTEGER REFERENCES equipment(id),
        mold_scan_required INTEGER NOT NULL DEFAULT 0,
        description TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ------------------------------------------------------------------ transactions
    """
    CREATE TABLE material_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_no TEXT NOT NULL UNIQUE,
        transaction_uuid TEXT NOT NULL UNIQUE,
        station_code TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('IN_PROGRESS','COMPLETED','REJECTED','CANCELLED','INTERRUPTED')),
        process_state TEXT NOT NULL,
        validation_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (validation_status IN ('PENDING','OK','NG')),
        operator_username TEXT NOT NULL DEFAULT '',
        bag_type_id INTEGER,
        bag_type_code TEXT NOT NULL,
        bag_type_name TEXT NOT NULL,
        material_type TEXT NOT NULL,
        colour TEXT NOT NULL,
        bag_barcode TEXT NOT NULL DEFAULT '',
        bag_lot TEXT NOT NULL DEFAULT '',
        bag_serial TEXT NOT NULL DEFAULT '',
        bag_weight_kg REAL NOT NULL DEFAULT 0,
        hopper_id INTEGER,
        hopper_code TEXT NOT NULL DEFAULT '',
        silo_id INTEGER,
        silo_code TEXT NOT NULL DEFAULT '',
        distribution_tank_id INTEGER,
        distribution_tank_code TEXT NOT NULL DEFAULT '',
        mold_id INTEGER,
        mold_code TEXT NOT NULL DEFAULT '',
        destination_id INTEGER,
        destination_code TEXT NOT NULL DEFAULT '',
        quantity_before_kg REAL,
        quantity_after_kg REAL,
        bag_result TEXT NOT NULL DEFAULT 'PENDING',
        colour_result TEXT NOT NULL DEFAULT 'PENDING',
        hopper_result TEXT NOT NULL DEFAULT 'PENDING',
        silo_result TEXT NOT NULL DEFAULT 'PENDING',
        distribution_result TEXT NOT NULL DEFAULT 'PENDING',
        mold_result TEXT NOT NULL DEFAULT 'PENDING',
        ng_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        confirmation_source TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        validated_at TEXT,
        authorized_at TEXT,
        completed_at TEXT,
        ended_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_material_transactions_started_at ON material_transactions (started_at)",
    "CREATE INDEX ix_material_transactions_bag_barcode ON material_transactions (bag_barcode)",
    "CREATE INDEX ix_material_transactions_status ON material_transactions (status)",
    """
    CREATE TABLE barcode_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id INTEGER REFERENCES material_transactions(id),
        scanned_at TEXT NOT NULL,
        stage TEXT NOT NULL,
        barcode TEXT NOT NULL,
        input_source TEXT NOT NULL,
        result TEXT NOT NULL CHECK (result IN ('OK','NG','IGNORED')),
        expected_value TEXT NOT NULL DEFAULT '',
        matched_equipment_id INTEGER,
        message TEXT NOT NULL DEFAULT '',
        operator_username TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX ix_barcode_scans_transaction ON barcode_scans (transaction_id)",
    "CREATE INDEX ix_barcode_scans_scanned_at ON barcode_scans (scanned_at)",
    # ------------------------------------------------------------------ inventory
    """
    CREATE TABLE tank_inventory (
        equipment_id INTEGER PRIMARY KEY REFERENCES equipment(id),
        quantity_kg REAL NOT NULL DEFAULT 0,
        last_transaction_id INTEGER,
        last_change_at TEXT NOT NULL,
        last_correction_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE inventory_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        equipment_id INTEGER NOT NULL REFERENCES equipment(id),
        changed_at TEXT NOT NULL,
        change_type TEXT NOT NULL CHECK (change_type IN ('INITIAL','LOADING','CORRECTION')),
        quantity_before_kg REAL NOT NULL,
        change_kg REAL NOT NULL,
        quantity_after_kg REAL NOT NULL,
        transaction_id INTEGER REFERENCES material_transactions(id),
        reason TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX ix_inventory_history_equipment ON inventory_history (equipment_id, changed_at)",
    # ------------------------------------------------------------------ configuration
    _CONFIG_TABLE.format(name="plc_configuration"),
    _CONFIG_TABLE.format(name="scanner_configuration"),
    _CONFIG_TABLE.format(name="mes_configuration"),
    _CONFIG_TABLE.format(name="app_settings"),
    # ------------------------------------------------------------------ MES queue
    """
    CREATE TABLE mes_sync_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id INTEGER NOT NULL UNIQUE REFERENCES material_transactions(id),
        idempotency_key TEXT NOT NULL UNIQUE,
        payload TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PENDING','SYNCING','SYNCED','FAILED')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_attempt_at TEXT,
        last_error TEXT NOT NULL DEFAULT '',
        last_http_status INTEGER,
        batch_id TEXT NOT NULL DEFAULT '',
        synced_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_mes_sync_queue_status ON mes_sync_queue (status, next_attempt_at)",
    # ------------------------------------------------------------------ users
    """
    CREATE TABLE application_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        full_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL CHECK (role IN ('OPERATOR','SUPERVISOR','ADMIN')),
        password_hash TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        last_login_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ------------------------------------------------------------------ logs
    """
    CREATE TABLE audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        username TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL,
        action TEXT NOT NULL,
        entity TEXT NOT NULL DEFAULT '',
        entity_id TEXT NOT NULL DEFAULT '',
        details TEXT NOT NULL DEFAULT '',
        old_value TEXT NOT NULL DEFAULT '',
        new_value TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX ix_audit_logs_occurred_at ON audit_logs (occurred_at)",
    """
    CREATE TABLE system_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
        source TEXT NOT NULL,
        event_type TEXT NOT NULL,
        message TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        transaction_id INTEGER
    )
    """,
    "CREATE INDEX ix_system_events_occurred_at ON system_events (occurred_at)",
]
