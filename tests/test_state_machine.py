"""Transaction state machine transitions."""

from __future__ import annotations

import pytest

from mobis.core.models import Stage
from mobis.core.process_state_machine import (
    TRANSITIONS,
    InvalidTransitionError,
    ProcessState as S,
    ProcessStateMachine,
)

SILO_PLAN = [Stage.BAG, Stage.HOPPER, Stage.SILO]


def _verified(plan=SILO_PLAN) -> ProcessStateMachine:
    sm = ProcessStateMachine()
    sm.select_bag_type(plan)
    for stage in plan:
        sm.stage_verified(stage)
    return sm


def test_every_state_has_transition_rules():
    assert set(TRANSITIONS) == set(S)


def test_happy_path_with_silo():
    sm = ProcessStateMachine()
    sm.select_bag_type(SILO_PLAN)
    assert sm.state == S.BAG_TYPE_SELECTED and sm.next_stage == Stage.BAG
    sm.stage_verified(Stage.BAG)
    assert sm.state == S.BAG_SCANNED
    sm.stage_verified(Stage.HOPPER)
    assert sm.state == S.HOPPER_VERIFIED
    sm.stage_verified(Stage.SILO)
    assert sm.state == S.SILO_VERIFIED and sm.next_stage is None
    sm.validation_passed()
    sm.loading_authorized()
    sm.loading_started()
    sm.loading_completed()
    assert sm.state == S.LOADING_COMPLETED
    sm.select_bag_type(SILO_PLAN)  # next transaction
    assert sm.state == S.BAG_TYPE_SELECTED and sm.completed_stages == []


def test_distribution_only_plan_skips_silo():
    sm = _verified([Stage.BAG, Stage.HOPPER, Stage.DISTRIBUTION_TANK])
    assert sm.state == S.DISTRIBUTION_VERIFIED
    sm.validation_passed()
    assert sm.state == S.VALIDATION_PASSED


def test_stages_must_follow_plan_order():
    sm = ProcessStateMachine()
    sm.select_bag_type(SILO_PLAN)
    with pytest.raises(InvalidTransitionError):
        sm.stage_verified(Stage.HOPPER)


def test_partial_validation_cannot_authorize():
    sm = ProcessStateMachine()
    sm.select_bag_type(SILO_PLAN)
    sm.stage_verified(Stage.BAG)
    sm.stage_verified(Stage.HOPPER)
    with pytest.raises(InvalidTransitionError):
        sm.validation_passed()
    with pytest.raises(InvalidTransitionError):
        sm.loading_authorized()


def test_cannot_complete_without_authorization():
    sm = _verified()
    sm.validation_passed()
    with pytest.raises(InvalidTransitionError):
        sm.loading_completed()


def test_failure_and_resume_returns_to_last_verified_stage():
    sm = ProcessStateMachine()
    sm.select_bag_type(SILO_PLAN)
    sm.stage_verified(Stage.BAG)
    sm.stage_failed(Stage.HOPPER)
    assert sm.state == S.VALIDATION_FAILED and sm.failed_stage == Stage.HOPPER
    sm.resume_after_failure()
    assert sm.state == S.BAG_SCANNED and sm.next_stage == Stage.HOPPER


def test_failure_on_bag_resumes_to_bag_type_selected():
    sm = ProcessStateMachine()
    sm.select_bag_type(SILO_PLAN)
    sm.stage_failed(Stage.BAG)
    sm.resume_after_failure()
    assert sm.state == S.BAG_TYPE_SELECTED and sm.next_stage == Stage.BAG


def test_cancel_allowed_during_loading_but_not_from_idle():
    sm = _verified()
    sm.validation_passed()
    sm.loading_authorized()
    sm.cancel("operator")
    assert sm.state == S.CANCELLED
    with pytest.raises(InvalidTransitionError):
        ProcessStateMachine().cancel()


def test_plan_must_start_with_bag():
    with pytest.raises(ValueError):
        ProcessStateMachine().select_bag_type([Stage.HOPPER])
    with pytest.raises(ValueError):
        ProcessStateMachine().select_bag_type([Stage.BAG, Stage.HOPPER, Stage.HOPPER])


def test_bag_type_cannot_change_mid_transaction():
    sm = ProcessStateMachine()
    sm.select_bag_type(SILO_PLAN)
    sm.stage_verified(Stage.BAG)
    with pytest.raises(InvalidTransitionError):
        sm.select_bag_type(SILO_PLAN)


def test_listener_receives_transitions():
    seen = []
    sm = ProcessStateMachine()
    sm.add_listener(lambda old, new: seen.append((old, new)))
    sm.select_bag_type(SILO_PLAN)
    assert seen == [(S.IDLE, S.BAG_TYPE_SELECTED)]
