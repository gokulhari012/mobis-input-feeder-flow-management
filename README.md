# MOBIS – Input Feeder Flow Management

Desktop application for the Industrial PC (IPC) of a plastic injection moulding
plant. It verifies with barcodes that the correct material (type and colour) is
loaded into the correct hopper and routed to the correct silo or distribution
tank, raises an alarm on the Mitsubishi PLC (SLMP over Ethernet) on any
mismatch, records every loading for traceability, monitors tank quantities and
synchronises the records with the MES.

Built with Python, PySide6 (Qt 6), SQLite / SQLAlchemy.

---

Username: admin
Password: admin or 12345678

## Contents

1. [Quick start](#1-quick-start)
2. [Operator workflow](#2-operator-workflow)
3. [Configuration (administrator)](#3-configuration-administrator)
4. [Safety and reliability design](#4-safety-and-reliability-design)
5. [Architecture](#5-architecture)
6. [Testing](#6-testing)
7. [Deployment on the IPC](#7-deployment-on-the-ipc)
8. [Open specifications](#8-open-specifications)

---

## 1. Quick start

Requirements: Windows 10/11 (other platforms work for development), Python 3.10 or
newer (developed and tested with Python 3.13, PySide6 6.10).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt

# Test without hardware (PLC + scanner simulation, local data folder):
python main.py --simulate --data-dir .\data
```

On the first start a database is created with the equipment of the documented
physical setup (4 hoppers, 3 silos, 4 distribution tanks and their routing) and
five **demo** bag types.

| Login | Password | Note |
|---|---|---|
| `admin` | `admin` | A new password must be set at the first login |

The operation screen is available without login (shared *Station Operator*
session). Configuration pages ask for a supervisor / administrator login.

### Command line options

| Option | Purpose |
|---|---|
| `--simulate` | Force the PLC and scanner simulators (no hardware needed) |
| `--data-dir PATH` | Location of the database, logs, backups and exports |
| `--no-demo-data` | Do not create the demo bag types in a new database |
| `--fullscreen` | Start in full-screen (kiosk) mode |

Data directory resolution: `--data-dir` → environment variable `MOBIS_DATA_DIR` →
`%PROGRAMDATA%\MOBIS` → `%LOCALAPPDATA%\MOBIS` → `~/.mobis`. It contains
`mobis.db`, `logs\`, `backups\`, `exports\` and `recovery\`. Only one instance can
use a data directory at a time.

### Trying the workflow in simulation

1. Start with `--simulate`, select **ABS-BLK – ABS - Black** on the operation screen.
2. In *Scanner Simulation* (bottom of the operation screen) send
   *Bag barcode – ABS - Black*, then *Hopper – Hopper 1*, then *Silo – Silo 2*.
3. Confirm with **LOADING COMPLETE** (default confirmation mode) – Silo 2 increases
   by 25 kg and the record is queued for MES.
4. Send *Hopper 2* instead of *Hopper 1* to see the NG alarm, reset it and rescan.

To exercise the full PLC handshake in the simulator, enter test addresses on the
*PLC (SLMP)* page, for example heartbeat `D100`, alarm `M200`, validation result
`D202` (enabled), hopper code `D203`, loading complete `M210`, shutter `M211`,
light curtain `M212`, confirmation mode *PLC loading-complete signal*. These are
examples for the simulator only – real addresses must come from the PLC
programmer.

---

## 2. Operator workflow

The operation screen keeps the scan field focused at all times – operators never
need to click it. A large banner shows what to do next (blue), OK (green),
NG / alarm (red) or a waiting condition (orange).

1. **Select the bag type.** The expected colour, hopper, silo and distribution tank
   appear in the *Verification* table.
2. **Scan the bag barcode** – bag type and colour are verified.
3. **Scan the hopper barcode.**
4. **Scan the silo barcode** (only when required for the bag type).
5. **Scan the distribution tank barcode** (only when required).
6. *(Optional, when enabled)* **Scan the mold / machine barcode.**
7. When every check of the logic matrix is OK, the loading authorisation is sent
   to the PLC, which opens the hopper shutter. Fill the material.
8. Completion is confirmed by the PLC (light curtain / shutter / complete signal) or
   by the operator (**LOADING COMPLETE**), depending on configuration. Only then is
   the tank quantity increased and the record saved for MES.
9. Scan the next bag – a new transaction starts automatically with the same bag
   type (or press **New Transaction**).

**On a mismatch (NG)** the PLC alarm is raised immediately, the reason is shown
and further scans are blocked. After **Reset Alarm** (or PLC acknowledgement,
depending on configuration) the operator rescans the correct item or cancels the
transaction. Every scan – OK, NG or ignored – is recorded.

**Cancel Transaction** is possible at any time; after authorisation it revokes
the PLC authorisation and the tank quantity is not changed.

---

## 3. Configuration (administrator)

Recommended order when commissioning:

| Page | What to configure |
|---|---|
| **Equipment** | Hoppers, silos, distribution tanks, molds: ID, name, barcode value printed on the label, PLC code, optional limit-switch signal, capacity. **Routing** tab: which hopper feeds which silo / tank / mold. |
| **Bag Type Mapping** | Material types: colour, colour code, bag weight, barcode identification rule (exact / prefix / contains / regex / material field) with a live barcode tester, assigned hopper / silo / distribution tank / mold and which of them must be scanned. Global bag barcode structure (delimited fields or regular expression with named groups `material`, `colour`, `lot`, `serial`, `weight`). |
| **Barcode Scanner** | HID keyboard wedge (default) or serial / COM port, terminator, prefix / suffix, length limits, duplicate-scan window, live test field. |
| **PLC (SLMP)** | Connection (IP, port, TCP/UDP, 3E/4E, binary/ASCII, series), heartbeat, alarm and reset behaviour, validation result / authorisation registers, loading confirmation signals, status, manual read/write test, simulator. See [`docs/PLC_INTERFACE.md`](docs/PLC_INTERFACE.md). |
| **MES Integration** | Base URL, endpoint, method, authentication, headers, payload layout and field mapping, interval, batch size, retries; queue with retry of failed records and payload viewer. |
| **Administration** | Users and roles, station settings, date/time display, mold verification, auto backup, backup / restore, log viewer, diagnostics. |
| **Tank Inventory** | Estimated quantities, history, audited corrections after a stock count. |

Roles: **Operator** – operation screen, history, tank levels. **Supervisor** –
additionally export, event / audit logs, MES sync and retry, forced completion.
**Administrator** – everything. Supervisor and administrator sessions log out
automatically after inactivity (configurable). All configuration and master data
changes are written to the audit log.

---

## 4. Safety and reliability design

* The **PLC stays in control** of shutters and interlocks; the PC sends validation
  results, authorisation and alarm requests only.
* Loading is authorised only when **every** planned check is OK (logic matrix of
  the specification) – partial validation can never authorise loading (enforced
  by the state machine).
* **No authorisation** while the PLC link / heartbeat is faulty, the database is
  unavailable, an alarm is active or a configured limit switch is not OK.
* PLC outputs are kept as a **desired state** and re-written after every
  reconnection; authorisation is revoked on cancel and on application exit.
* Every scan and transaction step is **written to the database before the process
  advances**. Completion, inventory update and MES queue entry are committed in
  **one database transaction**. If even that fails, an emergency JSON record is
  written to `recovery\`.
* Transactions left open by a crash are marked **INTERRUPTED** at the next start
  (quantity unchanged).
* SQLite runs in WAL mode with `synchronous=FULL`; write transactions use
  `BEGIN IMMEDIATE`. Schema changes are versioned migrations, with an automatic
  backup before an existing database is upgraded.
* **MES**: records are stored locally first (PENDING), sent in batches by a
  background worker, marked SYNCED on success. An unreachable MES never fails a
  record – it stays PENDING and is retried. HTTP errors count towards the maximum
  retry count (then FAILED, never deleted, manual retry). A record is claimed
  (SYNCING) inside a write transaction so it can never be sent twice in parallel,
  and `transaction_uuid` serves as the idempotency key.
* PLC, scanner (serial) and MES communication run in **background threads**; the
  GUI never blocks on network or serial I/O.

---

## 5. Architecture

```
main.py                      entry point
mobis/
  app/                       bootstrap, composition root (context.py), main window, navigation, styles
  core/                      domain models, validation engine (logic matrix), state machine,
                             loading monitor, transaction manager, inventory, mapping service
  database/                  SQLAlchemy models, SQLite engine, migrations/, repositories/, seed data
  hardware/                  SLMP protocol + client, PLC simulator, PLC manager, scanner manager
  integrations/              MES client, payload builder, sync manager, controller
  services/                  configuration, audit / events, authentication, backup, export, logging
  workers/                   QThread workers: PLC, serial scanner, MES sync
  ui/                        pages (dashboard, inventory, history, bag_mapping, equipment,
                             scanner, plc, mes, admin), dialogs, reusable widgets
  resources/icons/           SVG icons
docs/                        PLC interface, missing specifications
tests/                       pytest suite (incl. end-to-end with the PLC simulator)
```

Threads: GUI thread (UI, transaction manager, short SQLite writes) · PLC worker
(socket I/O, heartbeat, polling, reconnection) · serial scanner worker · MES
worker (HTTP). Communication between threads uses queued Qt signals only.

Database tables: `bag_types`, `equipment`, `equipment_mappings`, `barcode_scans`,
`material_transactions`, `tank_inventory`, `inventory_history`,
`plc_configuration`, `scanner_configuration`, `mes_configuration`, `app_settings`,
`mes_sync_queue`, `application_users`, `audit_logs`, `system_events`,
`schema_migrations`. Timestamps are stored as ISO-8601 UTC and shown in local time.

To change the schema: add `mobis/database/migrations/mNNNN_*.py` (plain SQLite
DDL), register it in `migrations/runner.py`, update `database/models.py`. A test
verifies that every ORM column exists in the migrated schema.

---

## 6. Testing

```powershell
python -m pytest
```

The suite (140 tests) covers the logic matrix (all 16 combinations of the
specification), bag identification and colour rules, equipment / routing
verification, state machine transitions, loading confirmation, inventory
updates and corrections, MES queue states / retries / isolation of rejected
records / idempotency, SLMP frame encoding against reference frames and client
round trips against a fake PLC (binary / ASCII, 3E / 4E), scanner input
handling, schema migrations, the transaction manager workflow, and an
end-to-end run with the real PLC worker thread and the built-in simulator.

---

## 7. Deployment on the IPC

* Install Python and the requirements in a virtual environment (as in the quick
  start), or package the application (e.g. PyInstaller:
  `pyinstaller --name MOBIS --windowed --add-data "mobis/resources/icons;mobis/resources/icons" main.py`
  – packaging has not been verified yet).
* Use the default data directory `%PROGRAMDATA%\MOBIS` (shared by all Windows
  users) and make sure the operator account can write to it.
* Start the application automatically at logon; enable *Start in full screen*
  under *Administration* for kiosk operation.
* Keep the IPC clock synchronised (NTP); configure the backup retention and copy
  backups to a network location if required.
* Disable the PLC simulator on the *PLC (SLMP)* page once the real PLC is
  configured; the header shows a **SIMULATION** badge while any simulator is active.

---

## 8. Open specifications

Scanner model, bag barcode format, PLC model and device addresses, MES API and
several process decisions have not been specified yet. They are configurable
placeholders – see [`docs/MISSING_SPECIFICATIONS.md`](docs/MISSING_SPECIFICATIONS.md)
and the proposed PLC handshake in [`docs/PLC_INTERFACE.md`](docs/PLC_INTERFACE.md).
