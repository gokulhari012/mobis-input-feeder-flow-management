"""Scanner input normalisation, HID classification, duplicate protection, serial framing."""

from __future__ import annotations

from dataclasses import replace

from mobis.core.barcode_parser import BarcodeNormalizer, decode_escapes
from mobis.core.config_models import ScannerConfig
from mobis.core.models import InputSource
from mobis.database.repositories import Repositories
from mobis.hardware.barcode_scanner import ScannerManager, classify_keyboard_input
from mobis.services.audit_service import AuditService, EventService
from mobis.services.configuration_service import ConfigurationService
from mobis.workers.scanner_worker import split_frames, terminators_for


def test_escape_sequences():
    assert decode_escapes("<STX>") == "\x02"
    assert decode_escapes(r"\x03\r\n") == "\x03\r\n"
    assert decode_escapes("]C1") == "]C1"


def test_normalizer_strips_prefix_suffix_and_controls():
    cfg = ScannerConfig(prefix="<STX>", suffix="<ETX>", min_length=3)
    normalizer = BarcodeNormalizer(cfg)
    assert normalizer.normalize("\x02ABS-BLK-0001\x03\r\n") == ("ABS-BLK-0001", "")
    assert normalizer.normalize("  HOP-01 \x07") == ("HOP-01", "")
    assert normalizer.normalize("AB")[0] is None
    assert normalizer.normalize("")[0] is None


def test_gs_separator_replacement_and_uppercase():
    normalizer = BarcodeNormalizer(ScannerConfig(gs_replacement="|", uppercase=True))
    assert normalizer.normalize("abs\x1dblk\x1d001") == ("ABS|BLK|001", "")


def test_keyboard_classification():
    assert classify_keyboard_input(12, 8.0, 50) == InputSource.SCANNER_HID
    assert classify_keyboard_input(12, 180.0, 50) == InputSource.MANUAL
    assert classify_keyboard_input(1, 0.0, 50) == InputSource.MANUAL


def test_serial_framing():
    buffer = bytearray(b"ABC\r\nDEF\rGHI\nPART")
    frames = split_frames(buffer, terminators_for(ScannerConfig(serial_terminator="CR_OR_LF")))
    assert frames == [b"ABC", b"DEF", b"GHI"] and buffer == bytearray(b"PART")
    custom = bytearray(b"\x02X1\x03\x02X2\x03")
    cfg = ScannerConfig(serial_terminator="CUSTOM", custom_terminator_hex="03")
    assert split_frames(custom, terminators_for(cfg)) == [b"\x02X1", b"\x02X2"]


def _manager(db, **scanner_kw):
    repos = Repositories()
    audit = AuditService(db, repos)
    config = ConfigurationService(db, repos, audit)
    if scanner_kw:
        config.save("scanner", replace(config.scanner(), **scanner_kw))
    return ScannerManager(config, EventService(db, repos))


def test_duplicate_scans_are_ignored(qapp, db):
    manager = _manager(db, duplicate_window_ms=60_000)
    received, rejected = [], []
    manager.barcode_scanned.connect(lambda code, src: received.append((code, src)))
    manager.rejected.connect(lambda raw, reason: rejected.append(reason))
    manager.submit_keyboard("HOP-01", 5.0, 6)
    manager.submit_keyboard("HOP-01", 5.0, 6)
    manager.submit_keyboard("SILO-02", 5.0, 7)
    assert received == [("HOP-01", "SCANNER_HID"), ("SILO-02", "SCANNER_HID")]
    assert rejected == ["Duplicate scan ignored."]


def test_manual_entry_can_be_disabled(qapp, db):
    manager = _manager(db, allow_manual_entry=False)
    received, rejected = [], []
    manager.barcode_scanned.connect(lambda code, src: received.append(code))
    manager.rejected.connect(lambda raw, reason: rejected.append(reason))
    manager.submit_keyboard("HOP-01", 250.0, 6)  # typed slowly by hand
    assert received == [] and "Manual" in rejected[0]


def test_test_mode_does_not_reach_the_process(qapp, db):
    manager = _manager(db)
    manager.test_mode = True
    received, tested = [], []
    manager.barcode_scanned.connect(lambda code, src: received.append(code))
    manager.test_scan.connect(lambda code, src, raw: tested.append(code))
    manager.submit_simulated("HOP-01")
    assert received == [] and tested == ["HOP-01"]
