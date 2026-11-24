"""Validation engine: logic matrix, bag identification and equipment verification."""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from mobis.core.config_models import BarcodeFormatConfig
from mobis.core.models import BarcodeMatchType, CheckItem, CheckResult, NgReason, Stage
from mobis.core.validation_engine import ValidationEngine

from .conftest import make_snapshot

OK, NG, NA, PENDING = CheckResult.OK, CheckResult.NG, CheckResult.NA, CheckResult.PENDING


# --------------------------------------------------------------------------- #
# Logic matrix (16 combinations from the specification)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("colour,hopper,silo,dist", list(itertools.product([NG, OK], repeat=4)))
def test_logic_matrix_overall_ok_only_when_all_ok(colour, hopper, silo, dist):
    results = {
        CheckItem.BAG: OK,
        CheckItem.COLOUR: colour,
        CheckItem.HOPPER: hopper,
        CheckItem.SILO: silo,
        CheckItem.DISTRIBUTION_TANK: dist,
        CheckItem.MOLD: NA,
    }
    expected = OK if (colour, hopper, silo, dist) == (OK, OK, OK, OK) else NG
    assert ValidationEngine.overall(results) == expected


def test_not_required_stage_counts_as_ok():
    assert ValidationEngine.overall([OK, OK, OK, NA, NA, NA]) == OK


def test_pending_blocks_overall_ok():
    assert ValidationEngine.overall([OK, OK, PENDING, NA]) == PENDING
    assert ValidationEngine.overall([OK, NG, PENDING]) == NG


# --------------------------------------------------------------------------- #
# Stage planning
# --------------------------------------------------------------------------- #
def test_stage_plan_follows_mapping(snapshot):
    engine = ValidationEngine(snapshot)
    assert engine.stage_plan(snapshot.bag_types[1]) == [Stage.BAG, Stage.HOPPER, Stage.SILO]
    assert engine.stage_plan(snapshot.bag_types[2]) == [Stage.BAG, Stage.HOPPER, Stage.DISTRIBUTION_TANK]
    initial = engine.initial_results(snapshot.bag_types[1])
    assert initial[CheckItem.DISTRIBUTION_TANK] == NA
    assert initial[CheckItem.SILO] == PENDING


def test_mold_stage_only_when_globally_enabled():
    bag = replace(make_snapshot().bag_types[1], mold_id=31, mold_scan_required=True)
    disabled = make_snapshot(bag_types={1: bag})
    enabled = make_snapshot(bag_types={1: bag}, mold_verification_enabled=True)
    assert Stage.MOLD not in ValidationEngine(disabled).stage_plan(bag)
    assert ValidationEngine(enabled).stage_plan(bag)[-1] == Stage.MOLD


def test_destination_is_final_storage_stage(snapshot):
    engine = ValidationEngine(snapshot)
    assert engine.destination(snapshot.bag_types[1]).code == "SILO-02"
    assert engine.destination(snapshot.bag_types[2]).code == "DT-02"


# --------------------------------------------------------------------------- #
# Example from the specification: ABS Black -> Hopper 1 -> Silo 2
# --------------------------------------------------------------------------- #
def test_specification_example_ok(snapshot):
    engine = ValidationEngine(snapshot)
    bag_type = snapshot.bag_types[1]
    bag = engine.validate_bag(bag_type, "ABS-BLK-000123")
    assert bag.ok
    assert engine.validate_equipment(Stage.HOPPER, bag_type, "HOP-01").result == OK
    assert engine.validate_equipment(Stage.SILO, bag_type, "SILO-02").result == OK


def test_specification_example_wrong_hopper_is_ng(snapshot):
    engine = ValidationEngine(snapshot)
    outcome = engine.validate_equipment(Stage.HOPPER, snapshot.bag_types[1], "HOP-02")
    assert outcome.result == NG
    assert outcome.ng_reason == NgReason.HOPPER_MISMATCH
    assert "expected Hopper 1" in outcome.message and "Hopper 2" in outcome.message


def test_wrong_equipment_category_is_ng(snapshot):
    outcome = ValidationEngine(snapshot).validate_equipment(Stage.HOPPER, snapshot.bag_types[1], "SILO-02")
    assert outcome.result == NG
    assert "Wrong equipment type" in outcome.message


def test_unknown_barcode_is_ng(snapshot):
    outcome = ValidationEngine(snapshot).validate_equipment(Stage.SILO, snapshot.bag_types[1], "XYZ-999")
    assert outcome.result == NG
    assert outcome.ng_reason == NgReason.UNKNOWN_BARCODE


def test_bag_barcode_scanned_at_equipment_stage(snapshot):
    outcome = ValidationEngine(snapshot).validate_equipment(Stage.HOPPER, snapshot.bag_types[1], "ABS-BLK-0001")
    assert outcome.result == NG
    assert "bag barcode" in outcome.message.lower()


def test_inactive_equipment_is_ng(snapshot):
    outcome = ValidationEngine(snapshot).validate_equipment(Stage.HOPPER, snapshot.bag_types[1], "HOP-99")
    assert outcome.result == NG


def test_equipment_barcode_is_case_insensitive(snapshot):
    """Keyboard-wedge scanners invert case when Caps Lock is on."""
    outcome = ValidationEngine(snapshot).validate_equipment(Stage.HOPPER, snapshot.bag_types[1], "hop-01")
    assert outcome.result == OK


# --------------------------------------------------------------------------- #
# Bag identification and colour
# --------------------------------------------------------------------------- #
def test_bag_of_other_type_and_colour_is_ng(snapshot):
    check = ValidationEngine(snapshot).validate_bag(snapshot.bag_types[1], "ABS-RED-000001")
    assert not check.ok
    assert check.bag.result == NG
    assert check.colour.result == NG
    assert check.identified_bag_type.code == "ABS-RED"
    assert "ABS - Red" in check.bag.message


def test_bag_of_other_material_same_colour(snapshot):
    check = ValidationEngine(snapshot).validate_bag(snapshot.bag_types[1], "PA6-BLK-000001")
    assert check.bag.result == NG
    assert check.colour.result == OK


def test_unknown_bag_barcode(snapshot):
    check = ValidationEngine(snapshot).validate_bag(snapshot.bag_types[1], "RANDOM-123")
    assert check.bag.result == NG and check.colour.result == NG
    assert check.failure.ng_reason == NgReason.BAG_MISMATCH


@pytest.mark.parametrize(
    "match_type,value,barcode,expected",
    [
        (BarcodeMatchType.EXACT, "ABC123", "abc123", True),
        (BarcodeMatchType.EXACT, "ABC123", "ABC1234", False),
        (BarcodeMatchType.PREFIX, "ABS-", "ABS-9", True),
        (BarcodeMatchType.PREFIX, "ABS-", "XABS-9", False),
        (BarcodeMatchType.CONTAINS, "BLK", "LOT1-BLK-22", True),
        (BarcodeMatchType.REGEX, r"ABS-\d{4}", "ABS-1234", True),
        (BarcodeMatchType.REGEX, r"ABS-\d{4}", "ABS-12345", False),
    ],
)
def test_identification_rules(match_type, value, barcode, expected):
    snap = make_snapshot()
    bag = replace(snap.bag_types[1], match_type=match_type, match_value=value)
    snap = make_snapshot(bag_types={1: bag})
    assert ValidationEngine(snap).validate_bag(bag, barcode).bag.ok is expected


def test_delimited_format_checks_colour_field():
    fmt = BarcodeFormatConfig(mode="DELIMITED", delimiter="|", field_material=1, field_colour=2,
                              field_lot=3, field_serial=4, field_weight=5, use_barcode_weight=True)
    base = make_snapshot()
    bag = replace(base.bag_types[1], match_type=BarcodeMatchType.MATERIAL_CODE, match_value="ABS")
    snap = make_snapshot(bag_types={1: bag}, barcode_format=fmt)
    engine = ValidationEngine(snap)

    good = engine.validate_bag(bag, "ABS|BLK|L2311|000123|24.5")
    assert good.ok
    assert good.parsed.lot == "L2311" and good.parsed.serial == "000123"
    assert good.weight_kg == pytest.approx(24.5)

    wrong_colour = engine.validate_bag(bag, "ABS|RED|L2311|000124|25")
    assert wrong_colour.bag.result == OK
    assert wrong_colour.colour.result == NG
    assert wrong_colour.failure.ng_reason == NgReason.COLOUR_MISMATCH

    too_short = engine.validate_bag(bag, "ABS|BLK")
    assert too_short.failure.ng_reason == NgReason.BARCODE_FORMAT


def test_regex_format_with_named_groups():
    fmt = BarcodeFormatConfig(mode="REGEX", regex=r"(?P<material>[A-Z0-9]+)-(?P<colour>[A-Z]+)-(?P<serial>\d+)")
    base = make_snapshot()
    bag = replace(base.bag_types[1], match_type=BarcodeMatchType.MATERIAL_CODE, match_value="ABS")
    engine = ValidationEngine(make_snapshot(bag_types={1: bag}, barcode_format=fmt))
    assert engine.validate_bag(bag, "ABS-BLK-0001").ok
    assert engine.validate_bag(bag, "ABS_BLK_0001").failure.ng_reason == NgReason.BARCODE_FORMAT


def test_duplicate_bag_is_rejected(snapshot):
    lookup = {"ABS-BLK-000001": "IFM01-20261124-0001"}.get
    engine = ValidationEngine(snapshot)
    check = engine.validate_bag(snapshot.bag_types[1], "ABS-BLK-000001", used_bag_lookup=lookup)
    assert check.failure.ng_reason == NgReason.DUPLICATE_BAG
    assert engine.validate_bag(snapshot.bag_types[1], "ABS-BLK-000002", used_bag_lookup=lookup).ok


# --------------------------------------------------------------------------- #
# Configuration consistency (routing)
# --------------------------------------------------------------------------- #
def test_valid_bag_types_have_no_configuration_errors(snapshot):
    engine = ValidationEngine(snapshot)
    for bag_type in snapshot.bag_types.values():
        assert engine.check_bag_type(bag_type) == []


def test_routing_violation_is_reported(snapshot):
    bad = replace(snapshot.bag_types[1], hopper_id=3)  # Hopper 3 does not feed Silo 2
    errors = ValidationEngine(snapshot).check_bag_type(bad)
    assert any("Routing" in e for e in errors)


def test_scan_required_without_assignment_is_reported(snapshot):
    bad = replace(snapshot.bag_types[1], silo_id=None, silo_scan_required=True)
    assert any("no silo" in e for e in ValidationEngine(snapshot).check_bag_type(bad))


def test_mold_compatibility(snapshot):
    snap = make_snapshot(mold_verification_enabled=True)
    engine = ValidationEngine(snap)
    any_mold = replace(snap.bag_types[1], mold_scan_required=True)
    assert engine.validate_equipment(Stage.MOLD, any_mold, "MLD-01").result == OK  # linked to Hopper 1
    assert engine.validate_equipment(Stage.MOLD, any_mold, "MLD-02").result == NG  # not linked
    specific = replace(any_mold, mold_id=32)
    assert any("does not feed" in e for e in engine.check_bag_type(specific))
