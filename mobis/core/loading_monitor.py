"""Loading confirmation logic.

After the PLC has been told that loading is authorised, the physical sequence
(hopper shutter opens, operator pours the material, light curtain detects the
material, shutter closes) is controlled entirely by the PLC.  The PC only
*observes* the outcome to decide when the transaction is complete and the tank
quantity may be increased.

Supported confirmation modes:

``PLC_SIGNAL``     the PLC sets a "loading complete" signal.  To guard against a
                   signal still latched from the previous cycle, completion is only
                   accepted after the signal has been observed *inactive* at least
                   once since authorisation (rising edge).
``SHUTTER_CYCLE``  completion = shutter opened and closed again; when a light
                   curtain address is configured, material must also have been
                   detected while the shutter was open.
``OPERATOR``       the operator confirms on screen (fallback until PLC signals
                   are available).  Shutter / light curtain signals, if
                   configured, are still shown for information.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional

from .config_models import PlcConfig


class LoadingEventType(str, Enum):
    STARTED = "STARTED"
    MATERIAL_DETECTED = "MATERIAL_DETECTED"
    COMPLETED = "COMPLETED"
    WAITING_SIGNAL_RESET = "WAITING_SIGNAL_RESET"
    SHUTTER_CLOSED_WITHOUT_MATERIAL = "SHUTTER_CLOSED_WITHOUT_MATERIAL"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class LoadingEvent:
    type: LoadingEventType
    message: str


MODES = ("OPERATOR", "PLC_SIGNAL", "SHUTTER_CYCLE")


def _u16(value: int) -> int:
    return int(value) & 0xFFFF


class LoadingMonitor:
    """Pure state tracker - feed it PLC signal values, it returns events."""

    def __init__(self, config: PlcConfig, normalize=lambda a: a.strip().upper()):
        mode = config.loading_confirmation_mode
        self.mode = mode if mode in MODES else "OPERATOR"
        self._cfg = config
        self._norm = normalize
        self.started_at: Optional[float] = None
        self.started = False
        self.material_detected = False
        self.completed = False
        self._shutter_open = False
        self._shutter_cycle_done = False
        self._complete_seen_inactive = False
        self._waiting_warned = False
        self._timeout_warned = False

    def _read(self, values: Mapping[str, Optional[int]], address: str) -> Optional[int]:
        address = (address or "").strip()
        if not address:
            return None
        return values.get(self._norm(address))

    def start(self, now: float) -> None:
        self.started_at = now

    def update(self, values: Mapping[str, Optional[int]], now: float) -> list[LoadingEvent]:
        if self.completed:
            return []
        cfg = self._cfg
        events: list[LoadingEvent] = []

        shutter = self._read(values, cfg.shutter_status_address)
        if shutter is not None:
            is_open = shutter == _u16(cfg.shutter_open_value)
            if is_open and not self._shutter_open:
                self._shutter_open = True
                if not self.started:
                    self.started = True
                    events.append(LoadingEvent(LoadingEventType.STARTED, "Hopper shutter opened."))
            elif not is_open and self._shutter_open:
                self._shutter_open = False
                self._shutter_cycle_done = True

        curtain = self._read(values, cfg.light_curtain_address)
        if curtain is not None and curtain == _u16(cfg.light_curtain_active_value) and not self.material_detected:
            self.material_detected = True
            events.append(LoadingEvent(LoadingEventType.MATERIAL_DETECTED, "Light curtain confirmed material flow."))
            if not self.started:
                self.started = True
                events.append(LoadingEvent(LoadingEventType.STARTED, "Material loading started."))

        if self.mode == "PLC_SIGNAL":
            complete = self._read(values, cfg.loading_complete_address)
            if complete is not None:
                if complete != _u16(cfg.loading_complete_value):
                    self._complete_seen_inactive = True
                elif self._complete_seen_inactive:
                    self.completed = True
                    events.append(LoadingEvent(LoadingEventType.COMPLETED, "PLC confirmed loading complete."))
                elif not self._waiting_warned:
                    self._waiting_warned = True
                    events.append(LoadingEvent(
                        LoadingEventType.WAITING_SIGNAL_RESET,
                        "Loading-complete signal is still active from the previous cycle; "
                        "waiting for the PLC to reset it.",
                    ))
        elif self.mode == "SHUTTER_CYCLE" and self._shutter_cycle_done:
            if cfg.light_curtain_address.strip() and not self.material_detected:
                self._shutter_cycle_done = False
                events.append(LoadingEvent(
                    LoadingEventType.SHUTTER_CLOSED_WITHOUT_MATERIAL,
                    "Shutter closed without light-curtain confirmation; loading not confirmed.",
                ))
            else:
                self.completed = True
                events.append(LoadingEvent(LoadingEventType.COMPLETED, "Shutter cycle completed."))

        if (
            not self.completed
            and not self._timeout_warned
            and self.started_at is not None
            and cfg.loading_timeout_min > 0
            and now - self.started_at > cfg.loading_timeout_min * 60
        ):
            self._timeout_warned = True
            events.append(LoadingEvent(
                LoadingEventType.TIMEOUT,
                f"Loading not confirmed within {cfg.loading_timeout_min} minutes.",
            ))
        return events
