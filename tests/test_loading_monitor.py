"""Loading confirmation (light curtain / shutter / PLC complete signal)."""

from __future__ import annotations

from mobis.core.config_models import PlcConfig
from mobis.core.loading_monitor import LoadingEventType as E, LoadingMonitor


def _types(events):
    return [e.type for e in events]


def test_plc_signal_requires_rising_edge():
    cfg = PlcConfig(loading_confirmation_mode="PLC_SIGNAL", loading_complete_address="M100")
    monitor = LoadingMonitor(cfg)
    monitor.start(0.0)
    # Signal still latched from the previous cycle -> must not complete.
    assert _types(monitor.update({"M100": 1}, 1.0)) == [E.WAITING_SIGNAL_RESET]
    assert monitor.update({"M100": 1}, 2.0) == []
    assert monitor.update({"M100": 0}, 3.0) == []
    assert _types(monitor.update({"M100": 1}, 4.0)) == [E.COMPLETED]
    assert monitor.completed
    assert monitor.update({"M100": 1}, 5.0) == []


def test_plc_signal_with_shutter_reports_start():
    cfg = PlcConfig(loading_confirmation_mode="PLC_SIGNAL", loading_complete_address="D10",
                    shutter_status_address="X10", light_curtain_address="X11")
    monitor = LoadingMonitor(cfg)
    monitor.start(0.0)
    assert monitor.update({"D10": 0, "X10": 0, "X11": 0}, 1.0) == []
    assert _types(monitor.update({"D10": 0, "X10": 1, "X11": 0}, 2.0)) == [E.STARTED]
    assert _types(monitor.update({"D10": 0, "X10": 1, "X11": 1}, 3.0)) == [E.MATERIAL_DETECTED]
    assert _types(monitor.update({"D10": 1, "X10": 0, "X11": 0}, 4.0)) == [E.COMPLETED]


def test_shutter_cycle_requires_material_when_light_curtain_configured():
    cfg = PlcConfig(loading_confirmation_mode="SHUTTER_CYCLE", shutter_status_address="X10",
                    light_curtain_address="X11")
    monitor = LoadingMonitor(cfg)
    monitor.start(0.0)
    monitor.update({"X10": 1, "X11": 0}, 1.0)
    assert _types(monitor.update({"X10": 0, "X11": 0}, 2.0)) == [E.SHUTTER_CLOSED_WITHOUT_MATERIAL]
    assert not monitor.completed
    monitor.update({"X10": 1, "X11": 0}, 3.0)
    monitor.update({"X10": 1, "X11": 1}, 4.0)
    assert _types(monitor.update({"X10": 0, "X11": 0}, 5.0)) == [E.COMPLETED]


def test_shutter_cycle_without_light_curtain():
    cfg = PlcConfig(loading_confirmation_mode="SHUTTER_CYCLE", shutter_status_address="X10")
    monitor = LoadingMonitor(cfg)
    monitor.start(0.0)
    assert _types(monitor.update({"X10": 1}, 1.0)) == [E.STARTED]
    assert _types(monitor.update({"X10": 0}, 2.0)) == [E.COMPLETED]


def test_operator_mode_never_completes_automatically():
    cfg = PlcConfig(loading_confirmation_mode="OPERATOR", shutter_status_address="X10",
                    loading_complete_address="M1")
    monitor = LoadingMonitor(cfg)
    monitor.start(0.0)
    monitor.update({"X10": 1, "M1": 0}, 1.0)
    events = monitor.update({"X10": 0, "M1": 1}, 2.0)
    assert E.COMPLETED not in _types(events)


def test_timeout_warning_is_emitted_once():
    cfg = PlcConfig(loading_confirmation_mode="OPERATOR", loading_timeout_min=1)
    monitor = LoadingMonitor(cfg)
    monitor.start(0.0)
    assert monitor.update({}, 30.0) == []
    assert _types(monitor.update({}, 61.0)) == [E.TIMEOUT]
    assert monitor.update({}, 120.0) == []
