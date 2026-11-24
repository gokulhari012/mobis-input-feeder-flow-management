"""End-to-end: real services, PLC worker thread and the built-in PLC simulator."""

from __future__ import annotations

import time
from dataclasses import replace

from PySide6.QtCore import QCoreApplication

from mobis.app.context import build_context
from mobis.core.process_state_machine import ProcessState as S
from mobis.services.paths import AppPaths


def _wait(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_full_loading_cycle_with_simulated_plc(qapp, tmp_path):
    ctx = build_context(AppPaths(tmp_path / "data"), simulate=True)
    plc_cfg = replace(
        ctx.config.plc(),
        heartbeat_address="D100", heartbeat_interval_ms=200,
        alarm_address="M200", alarm_code_address="D201",
        validation_result_enabled=True, validation_result_address="D202",
        hopper_code_address="D203", destination_code_address="D204",
        loading_confirmation_mode="PLC_SIGNAL", loading_complete_address="M210",
        shutter_status_address="M211", light_curtain_address="M212",
        sim_auto_loading=True, sim_step_delay_s=0.2, poll_interval_ms=100,
    )
    ctx.config.save("plc", plc_cfg, "test")
    ctx.start_services()
    try:
        assert _wait(lambda: ctx.plc.connected and ctx.plc.is_ready()[0]), ctx.plc.is_ready()
        tm = ctx.transactions
        bag_type = next(b for b in tm.bag_type_options() if b.code == "PC-GRY")
        assert tm.select_bag_type(bag_type.id)[0]

        # Wrong distribution tank first: alarm must reach the (simulated) PLC.
        tm.process_scan("PC-GRY-000001", "SIMULATION")
        tm.process_scan("HOP-03", "SIMULATION")
        tm.process_scan("DT-03", "SIMULATION")
        assert tm.state == S.VALIDATION_FAILED
        assert _wait(lambda: ctx.plc.sim_memory.read("M200") == 1)
        assert ctx.plc.sim_memory.read("D201") == 5  # NgReason.DISTRIBUTION_MISMATCH
        assert ctx.plc.sim_memory.read("D202") == plc_cfg.validation_ng_value

        assert tm.reset_alarm()[0]
        assert _wait(lambda: ctx.plc.sim_memory.read("M200") == 0)
        tm.process_scan("DT-02", "SIMULATION")

        # Authorisation written to the PLC, simulator runs shutter / light curtain / complete.
        assert _wait(lambda: tm.state in (S.LOADING_AUTHORIZED, S.LOADING_IN_PROGRESS))
        assert _wait(lambda: ctx.plc.sim_memory.read("D203") == 3)  # Hopper 3 PLC code
        assert _wait(lambda: tm.state == S.LOADING_COMPLETED, timeout=20), tm.ctx.banner
        assert _wait(lambda: ctx.plc.sim_memory.read("D203") == 0)  # authorisation revoked

        tank = next(e for e in ctx.mapping.list_equipment() if e.code == "DT-02")
        assert ctx.inventory.quantity(tank.id) == 25.0
        assert ctx.mes.refresh_counts()["PENDING"] == 1
        assert ctx.plc.sim_memory.read("D100") in (0, 1)  # heartbeat toggling
    finally:
        ctx.shutdown()
