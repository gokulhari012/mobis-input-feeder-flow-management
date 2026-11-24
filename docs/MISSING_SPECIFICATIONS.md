# Missing Specifications and Open Decisions

The application was built from the *Input Feeder Traceability* and *Barcode
Integration for Injection Mold* documents. The items below were **not specified**.
Nothing has been invented for them: every item is configurable, and the
current placeholder behaviour is listed so the system can be commissioned as
soon as the information is available.

## Hardware and interfaces

| # | Item | Needed from | Current placeholder | Where to configure |
|---|---|---|---|---|
| 1 | Wireless barcode scanner model and output mode (USB HID keyboard wedge or virtual COM port), configured prefix / suffix / terminator | Scanner supplier / IT | HID keyboard wedge, Enter terminator, no prefix / suffix | *Barcode Scanner* page |
| 2 | Mitsubishi PLC model / series, IP address, SLMP port, TCP or UDP, 3E / 4E frame, binary / ASCII | PLC programmer | Built-in PLC simulator enabled; IP / port empty | *PLC (SLMP)* > Connection |
| 3 | PLC device address of every signal (heartbeat, alarm, validation result, hopper / destination code, loading complete, shutter, light curtain, alarm acknowledgement) and their values | PLC programmer | All addresses empty (no writes) | *PLC (SLMP)* > Signals & Handshake – see `PLC_INTERFACE.md` |
| 4 | Agreement on the handshake (who resets the alarm, which signal confirms loading, behaviour on heartbeat loss) | PLC programmer / safety engineer | Operator resets alarm; operator confirms loading | *PLC (SLMP)* > Signals & Handshake |
| 5 | PLC codes identifying each hopper and destination tank | PLC programmer | Hoppers 1–4, silos 11–13, tanks 21–24 | *Equipment* > PLC code |
| 6 | "Limit switch OK" of the flow chart: which device per silo / tank, and the OK value | PLC programmer / mechanical | Not configured (check skipped) | *Equipment* > Limit switch / ready signal |

## Barcodes

| # | Item | Needed from | Current placeholder | Where to configure |
|---|---|---|---|---|
| 7 | Bag barcode label structure (fields, separator, colour code, lot, serial, weight) with sample labels per material | Material supplier / quality | Opaque barcode, bag types identified by prefix rule | *Bag Type Mapping* > Bag Barcode Format and each bag type's rule |
| 8 | Is every bag barcode unique (serial number per bag)? | Material supplier | Assumed unique: a barcode already used in a completed loading is rejected | *Bag Type Mapping* > "Reject already loaded bags" |
| 9 | Barcode values printed on the hopper, silo, distribution tank (and mold) labels | Production engineering | Equipment ID used as barcode (`HOP-01`, `SILO-02`, `DT-01` …) | *Equipment* > Barcode value |

## MES

| # | Item | Needed from | Current placeholder | Where to configure |
|---|---|---|---|---|
| 10 | MES API documentation: base URL, endpoint(s), HTTP method, authentication | MES team / IT | Disabled; generic JSON POST client | *MES Integration* |
| 11 | Payload schema and field names, batch support, maximum batch size | MES team | Envelope `{source, station_code, batch_id, sent_at, record_count, records:[…]}`; configurable field mapping / array mode | *MES Integration* > Request Format |
| 12 | Success / error response format, duplicate handling (idempotency) | MES team | HTTP 2xx = success, 409 = already received; `transaction_uuid` per record is the idempotency key | *MES Integration* |
| 13 | Which transactions MES wants (completed only, or also rejected / cancelled) | MES team / quality | Completed only | *MES Integration* > Synchronisation |
| 14 | Station / transaction ID convention | MES team | `<station code>-<yyyymmdd>-<seq>` (e.g. `IFM01-20261124-0001`) | *Administration* > Station code |

## Process and master data

| # | Item | Needed from | Current placeholder | Where to configure |
|---|---|---|---|---|
| 15 | Real material list: bag types, colours, weights, barcode rules and hopper / silo / tank assignment | Production planning | Five **DEMO** bag types (ABS-BLK, PP-NAT, PC-GRY, PA6-BLK, ABS-RED) | *Bag Type Mapping* (delete / deactivate the demo entries) |
| 16 | Tank capacities and initial quantities | Production | Silos 14 000 kg, distribution tanks 4 000 kg (from the document), initial 0 kg | *Equipment* > Capacity; *Tank Inventory* > Correct Quantity |
| 17 | Material consumption: the PC only knows what is loaded. Is a consumption / level signal available, or are periodic stock counts used? | Production / PLC programmer | Quantity only increases; administrators correct it after stock counts (audited) | *Tank Inventory* |
| 18 | If a bag type is mapped to both a silo and a distribution tank, which one receives the quantity? (Not the case in the documented layout.) | Production engineering | Distribution tank | – (code: `ValidationEngine.destination`) |
| 19 | Is mold / machine verification (second document) in scope for this station? Mold barcodes and hopper → mold routing | Production engineering | Disabled; fully implemented when enabled | *Administration* > Application; *Equipment* > Routing |
| 20 | One bag per transaction (scan, verify, fill, confirm) – confirm the operating practice | Production | One bag per transaction; the next bag scan starts the next transaction | – |
| 21 | Operator identification policy: shared station session or individual operator login | Production / quality | Shared station session; supervisors / admins log in on demand | *Administration* > Application |

## IT and operation

| # | Item | Needed from | Current placeholder | Where to configure |
|---|---|---|---|---|
| 22 | IPC screen resolution and kiosk requirements | IT / production | Maximised window; optional full-screen kiosk mode | *Administration* > Application, `--fullscreen` |
| 23 | Backup destination (local only or network share) and retention | IT | Daily automatic backup in `<data dir>\backups`, 14 kept | *Administration* > Database & Backup |
| 24 | Time synchronisation of the IPC (NTP server) | IT | Windows time settings | Windows |
| 25 | Storage of the MES credentials (plain configuration database vs. Windows credential store) | IT security | Stored in the local database, masked in the UI and audit log | – |
