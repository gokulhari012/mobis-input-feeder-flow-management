"""Typed configuration sections.

Each dataclass is persisted field-by-field by
:class:`mobis.services.configuration_service.ConfigurationService`.  New fields
can be added in future versions simply by declaring them with a default value -
existing installations pick up the default automatically (no migration needed).

IMPORTANT: no plant specific values (PLC IP address, device addresses, MES
endpoints, barcode structures) are hard coded here.  Fields that depend on
missing hardware / API specifications default to empty and must be configured
on site.  See ``docs/MISSING_SPECIFICATIONS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# PLC (Mitsubishi SLMP)
# --------------------------------------------------------------------------- #
@dataclass
class PlcConfig:
    # --- Connection -------------------------------------------------------
    enabled: bool = True
    simulation_mode: bool = True          # use the built-in PLC simulator
    host: str = ""                        # PLC IP address (site specific)
    port: int = 0                         # SLMP port configured in GX Works
    transport: str = "TCP"                # TCP | UDP
    frame_type: str = "3E"                # 3E | 4E
    data_code: str = "BINARY"             # BINARY | ASCII
    plc_series: str = "Q_L"               # Q_L | IQ_R | IQ_F
    network_no: int = 0
    pc_no: int = 255                      # 0xFF = own station
    module_io_no: int = 1023              # 0x03FF = own CPU
    station_no: int = 0
    monitoring_timer: int = 16            # x 250 ms (16 = 4 s)
    connect_timeout_s: float = 3.0
    io_timeout_s: float = 3.0
    reconnect_interval_s: float = 5.0
    poll_interval_ms: int = 300
    signal_max_age_s: float = 3.0         # cached PLC inputs older than this are "unknown"

    # --- Heartbeat --------------------------------------------------------
    heartbeat_address: str = ""           # PC -> PLC life signal
    heartbeat_interval_ms: int = 1000
    heartbeat_mode: str = "TOGGLE"        # TOGGLE (0/1) | COUNTER (0..32767)
    plc_watchdog_address: str = ""        # optional PLC -> PC life counter
    plc_watchdog_timeout_s: float = 5.0

    # --- Alarm ------------------------------------------------------------
    alarm_address: str = ""
    alarm_on_value: int = 1
    alarm_off_value: int = 0
    alarm_code_address: str = ""          # optional: NG reason code (see NgReason)
    alarm_reset_mode: str = "OPERATOR"    # OPERATOR | PLC_ACK | OPERATOR_OR_PLC
    alarm_ack_address: str = ""           # PLC -> PC acknowledgement signal
    alarm_ack_value: int = 1
    alarm_reset_command_address: str = "" # optional PC -> PLC reset command (pulsed)
    alarm_reset_command_value: int = 1
    alarm_reset_pulse_ms: int = 500

    # --- Validation result / loading authorisation -------------------------
    validation_result_enabled: bool = False
    validation_result_address: str = ""
    validation_ok_value: int = 1
    validation_ng_value: int = 2
    validation_idle_value: int = 0
    hopper_code_address: str = ""         # PC -> PLC: PLC code of the authorised hopper
    destination_code_address: str = ""    # PC -> PLC: PLC code of the destination tank

    # --- Loading confirmation ----------------------------------------------
    loading_confirmation_mode: str = "OPERATOR"  # OPERATOR | PLC_SIGNAL | SHUTTER_CYCLE
    loading_complete_address: str = ""
    loading_complete_value: int = 1
    shutter_status_address: str = ""
    shutter_open_value: int = 1
    light_curtain_address: str = ""
    light_curtain_active_value: int = 1
    loading_timeout_min: int = 30

    # --- Simulator ---------------------------------------------------------
    sim_auto_loading: bool = True
    sim_step_delay_s: float = 2.0

    def connection_signature(self) -> tuple:
        """Values that require a reconnect when changed."""
        return (
            self.enabled, self.simulation_mode, self.host, self.port, self.transport,
            self.frame_type, self.data_code, self.plc_series, self.network_no,
            self.pc_no, self.module_io_no, self.station_no, self.monitoring_timer,
            self.connect_timeout_s, self.io_timeout_s,
        )

    def authorization_outputs(self) -> list[str]:
        out = [self.hopper_code_address, self.destination_code_address]
        if self.validation_result_enabled:
            out.append(self.validation_result_address)
        return [a.strip() for a in out if a and a.strip()]

    def input_addresses(self) -> list[str]:
        """PLC -> PC signals that are polled continuously."""
        candidates = [
            self.plc_watchdog_address,
            self.alarm_ack_address,
            self.loading_complete_address,
            self.shutter_status_address,
            self.light_curtain_address,
        ]
        return [a.strip() for a in candidates if a and a.strip()]


# --------------------------------------------------------------------------- #
# Barcode scanner
# --------------------------------------------------------------------------- #
@dataclass
class ScannerConfig:
    mode: str = "HID_KEYBOARD"            # HID_KEYBOARD | SERIAL | SIMULATION

    # Keyboard wedge (HID) mode
    hid_terminator: str = "ENTER"         # ENTER | TAB | NONE (idle timeout)
    hid_max_interkey_ms: int = 50         # faster than this => scanner, slower => manual typing
    hid_idle_submit_ms: int = 150         # used when terminator is NONE
    allow_manual_entry: bool = True

    # Serial / virtual COM port mode
    serial_port: str = ""
    baud_rate: int = 9600
    data_bits: int = 8
    parity: str = "N"                     # N | E | O | M | S
    stop_bits: str = "1"                  # 1 | 1.5 | 2
    serial_terminator: str = "CR_OR_LF"   # CR_OR_LF | CR | LF | CRLF | NONE | CUSTOM
    custom_terminator_hex: str = ""       # e.g. "03" for ETX
    serial_idle_submit_ms: int = 100      # used when terminator is NONE
    encoding: str = "utf-8"               # utf-8 | ascii | latin-1
    serial_reconnect_s: float = 3.0

    # Data format (applied to every input source)
    prefix: str = ""                      # stripped if present; supports <STX>, \x02, \r
    suffix: str = ""
    strip_whitespace: bool = True
    remove_control_chars: bool = True
    gs_replacement: str = ""              # replacement for GS (0x1D) separators, e.g. "|"
    uppercase: bool = False
    min_length: int = 3
    max_length: int = 128
    duplicate_window_ms: int = 1500       # identical scans within this window are ignored


# --------------------------------------------------------------------------- #
# Bag barcode structure (identification rules)
# --------------------------------------------------------------------------- #
@dataclass
class BarcodeFormatConfig:
    mode: str = "NONE"                    # NONE (opaque) | DELIMITED | REGEX
    delimiter: str = "|"
    field_material: int = 0               # 1-based field positions, 0 = not present
    field_colour: int = 0
    field_lot: int = 0
    field_serial: int = 0
    field_weight: int = 0
    regex: str = ""                       # named groups: material, colour, lot, serial, weight
    use_barcode_weight: bool = False      # use weight encoded in barcode instead of bag type weight
    prevent_duplicate_bag: bool = True    # reject a bag barcode already used in a completed loading


# --------------------------------------------------------------------------- #
# MES integration
# --------------------------------------------------------------------------- #
@dataclass
class MesConfig:
    enabled: bool = False
    base_url: str = ""
    endpoint: str = ""
    http_method: str = "POST"             # POST | PUT | PATCH
    auth_type: str = "NONE"               # NONE | BEARER | BASIC | API_KEY
    api_token: str = ""
    username: str = ""
    password: str = ""
    api_key_header: str = "X-API-Key"
    headers_json: str = "{}"
    timeout_s: float = 10.0
    verify_tls: bool = True
    sync_interval_s: int = 30
    batch_size: int = 50
    retry_interval_s: int = 60
    max_retry_count: int = 5
    payload_mode: str = "ENVELOPE"        # ENVELOPE | ARRAY
    records_key: str = "records"
    field_mapping_json: str = "{}"        # {"internal_field": "mesField"}; "" excludes a field
    health_endpoint: str = ""
    treat_409_as_synced: bool = True
    include_rejected: bool = False
    include_cancelled: bool = False


# --------------------------------------------------------------------------- #
# General application settings
# --------------------------------------------------------------------------- #
@dataclass
class AppSettings:
    station_code: str = "IFM01"
    station_name: str = "Input Feeder Station"
    plant_name: str = ""
    require_operator_login: bool = False
    admin_session_timeout_min: int = 10
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M:%S"
    mold_verification_enabled: bool = False
    auto_new_transaction: bool = True
    confirm_cancel: bool = True
    history_max_rows: int = 2000
    auto_backup_enabled: bool = True
    auto_backup_interval_h: int = 24
    backup_retention_count: int = 14
    log_level: str = "INFO"
    kiosk_fullscreen: bool = False
