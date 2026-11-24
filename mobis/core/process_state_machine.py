"""Transaction state machine.

The sequence of scanning stages is configurable per bag type (``plan``), e.g.
``[BAG, HOPPER, SILO]`` or ``[BAG, HOPPER, DISTRIBUTION_TANK]``.  The machine
enforces that stages are verified strictly in plan order and that loading can
only be authorised after *every* planned stage has been verified - partial
validation can never reach ``LOADING_AUTHORIZED``.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional

from .models import Stage


class ProcessState(str, Enum):
    IDLE = "IDLE"
    BAG_TYPE_SELECTED = "BAG_TYPE_SELECTED"
    BAG_SCANNED = "BAG_SCANNED"
    HOPPER_VERIFIED = "HOPPER_VERIFIED"
    SILO_VERIFIED = "SILO_VERIFIED"
    DISTRIBUTION_VERIFIED = "DISTRIBUTION_VERIFIED"
    MOLD_VERIFIED = "MOLD_VERIFIED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    LOADING_AUTHORIZED = "LOADING_AUTHORIZED"
    LOADING_IN_PROGRESS = "LOADING_IN_PROGRESS"
    LOADING_COMPLETED = "LOADING_COMPLETED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CANCELLED = "CANCELLED"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


S = ProcessState

#: State reached when a stage is verified.
STAGE_STATE: dict[Stage, ProcessState] = {
    Stage.BAG: S.BAG_SCANNED,
    Stage.HOPPER: S.HOPPER_VERIFIED,
    Stage.SILO: S.SILO_VERIFIED,
    Stage.DISTRIBUTION_TANK: S.DISTRIBUTION_VERIFIED,
    Stage.MOLD: S.MOLD_VERIFIED,
}

TRANSITIONS: dict[ProcessState, frozenset[ProcessState]] = {
    S.IDLE: frozenset({S.BAG_TYPE_SELECTED}),
    S.BAG_TYPE_SELECTED: frozenset({S.BAG_TYPE_SELECTED, S.BAG_SCANNED, S.VALIDATION_FAILED, S.CANCELLED, S.IDLE}),
    S.BAG_SCANNED: frozenset({S.HOPPER_VERIFIED, S.VALIDATION_FAILED, S.CANCELLED}),
    S.HOPPER_VERIFIED: frozenset(
        {S.SILO_VERIFIED, S.DISTRIBUTION_VERIFIED, S.MOLD_VERIFIED, S.VALIDATION_PASSED, S.VALIDATION_FAILED, S.CANCELLED}
    ),
    S.SILO_VERIFIED: frozenset(
        {S.DISTRIBUTION_VERIFIED, S.MOLD_VERIFIED, S.VALIDATION_PASSED, S.VALIDATION_FAILED, S.CANCELLED}
    ),
    S.DISTRIBUTION_VERIFIED: frozenset({S.MOLD_VERIFIED, S.VALIDATION_PASSED, S.VALIDATION_FAILED, S.CANCELLED}),
    S.MOLD_VERIFIED: frozenset({S.VALIDATION_PASSED, S.VALIDATION_FAILED, S.CANCELLED}),
    S.VALIDATION_PASSED: frozenset({S.LOADING_AUTHORIZED, S.VALIDATION_FAILED, S.CANCELLED}),
    S.LOADING_AUTHORIZED: frozenset({S.LOADING_IN_PROGRESS, S.LOADING_COMPLETED, S.CANCELLED}),
    S.LOADING_IN_PROGRESS: frozenset({S.LOADING_COMPLETED, S.CANCELLED}),
    S.LOADING_COMPLETED: frozenset({S.BAG_TYPE_SELECTED, S.IDLE}),
    S.VALIDATION_FAILED: frozenset(
        {S.BAG_TYPE_SELECTED, S.BAG_SCANNED, S.HOPPER_VERIFIED, S.SILO_VERIFIED,
         S.DISTRIBUTION_VERIFIED, S.MOLD_VERIFIED, S.CANCELLED}
    ),
    S.CANCELLED: frozenset({S.BAG_TYPE_SELECTED, S.IDLE}),
}

#: States in which barcode scans are accepted as verification input.
SCANNING_STATES = frozenset(
    {S.BAG_TYPE_SELECTED, S.BAG_SCANNED, S.HOPPER_VERIFIED, S.SILO_VERIFIED, S.DISTRIBUTION_VERIFIED, S.MOLD_VERIFIED}
)
#: States in which the hopper may be (or is being) filled.
LOADING_STATES = frozenset({S.LOADING_AUTHORIZED, S.LOADING_IN_PROGRESS})


class InvalidTransitionError(RuntimeError):
    pass


Listener = Callable[[ProcessState, ProcessState], None]


class ProcessStateMachine:
    def __init__(self) -> None:
        self._state = S.IDLE
        self._plan: list[Stage] = []
        self._done: list[Stage] = []
        self._failed_stage: Optional[Stage] = None
        self._history: list[tuple[ProcessState, ProcessState, str]] = []
        self._listeners: list[Listener] = []

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> ProcessState:
        return self._state

    @property
    def plan(self) -> list[Stage]:
        return list(self._plan)

    @property
    def completed_stages(self) -> list[Stage]:
        return list(self._done)

    @property
    def failed_stage(self) -> Optional[Stage]:
        return self._failed_stage

    @property
    def history(self) -> list[tuple[ProcessState, ProcessState, str]]:
        return list(self._history)

    @property
    def next_stage(self) -> Optional[Stage]:
        for stage in self._plan:
            if stage not in self._done:
                return stage
        return None

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def can_transition(self, target: ProcessState) -> bool:
        return target in TRANSITIONS[self._state]

    # ------------------------------------------------------------------ #
    def _require(self, target: ProcessState) -> None:
        if not self.can_transition(target):
            raise InvalidTransitionError(f"Transition {self._state.value} -> {target.value} is not allowed")

    def _go(self, target: ProcessState, reason: str = "") -> None:
        self._require(target)
        previous = self._state
        self._state = target
        self._history.append((previous, target, reason))
        for listener in list(self._listeners):
            listener(previous, target)

    # ------------------------------------------------------------------ #
    def select_bag_type(self, plan: list[Stage]) -> None:
        if not plan or plan[0] != Stage.BAG:
            raise ValueError("A stage plan must start with the BAG stage")
        if len(set(plan)) != len(plan):
            raise ValueError("A stage plan cannot contain duplicate stages")
        self._require(S.BAG_TYPE_SELECTED)
        self._plan = list(plan)
        self._done = []
        self._failed_stage = None
        self._go(S.BAG_TYPE_SELECTED, "bag type selected")

    def stage_verified(self, stage: Stage) -> None:
        expected = self.next_stage
        if stage != expected:
            raise InvalidTransitionError(
                f"Stage {stage.value} verified out of order (expected {expected.value if expected else 'none'})"
            )
        target = STAGE_STATE[stage]
        self._require(target)
        self._done.append(stage)
        self._go(target, f"{stage.value} verified")

    def stage_failed(self, stage: Stage) -> None:
        self._require(S.VALIDATION_FAILED)
        self._failed_stage = stage
        self._go(S.VALIDATION_FAILED, f"{stage.value} NG")

    def resume_after_failure(self) -> None:
        """Return to the last verified stage after the alarm has been reset."""
        if self._state != S.VALIDATION_FAILED:
            raise InvalidTransitionError("resume_after_failure is only valid in VALIDATION_FAILED")
        target = STAGE_STATE[self._done[-1]] if self._done else S.BAG_TYPE_SELECTED
        self._failed_stage = None
        self._go(target, "resume after alarm reset")

    def validation_passed(self) -> None:
        if not self._plan or self.next_stage is not None:
            raise InvalidTransitionError("Validation cannot pass before all planned stages are verified")
        self._go(S.VALIDATION_PASSED, "all stages verified")

    def loading_authorized(self) -> None:
        self._go(S.LOADING_AUTHORIZED, "PLC authorisation sent")

    def loading_started(self) -> None:
        self._go(S.LOADING_IN_PROGRESS, "loading started")

    def loading_completed(self) -> None:
        self._go(S.LOADING_COMPLETED, "loading confirmed")

    def cancel(self, reason: str = "") -> None:
        self._go(S.CANCELLED, reason or "cancelled")

    def reset(self) -> None:
        """Return to IDLE (only from states where no transaction is open)."""
        if self._state == S.IDLE:
            return
        self._go(S.IDLE, "reset")
        self._plan, self._done, self._failed_stage = [], [], None

    def force_idle(self) -> None:
        """Unconditional reset - used on start-up recovery only."""
        previous = self._state
        self._state = S.IDLE
        self._plan, self._done, self._failed_stage = [], [], None
        self._history.append((previous, S.IDLE, "forced"))
