"""Centralised material verification logic.

The engine works on an immutable :class:`ConfigSnapshot` (equipment master,
routing links, bag types and barcode format) taken when a transaction starts,
so that configuration edits by an administrator can never change the rules of
a transaction that is already in progress.

Logic matrix (from the "Input Feeder Traceability" specification): the overall
result is OK only when bag type, colour, hopper, silo, distribution tank (and
mold, when enabled) are all OK.  Stages that are not required for the selected
bag type are N/A and do not block the result.  Any NG makes the overall NG.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .barcode_parser import (
    BagBarcodeParser,
    bag_type_matches,
    colour_matches,
    colours_equal,
    describe_rule,
    validate_rule,
)
from .config_models import BarcodeFormatConfig
from .models import (
    STAGE_NG_REASON,
    BagCheck,
    BagType,
    CheckItem,
    CheckOutcome,
    CheckResult,
    Equipment,
    EquipmentCategory,
    NgReason,
    Stage,
)

#: Callable used for duplicate bag protection: returns a description of the
#: earlier completed loading that used the barcode, or ``None``.
UsedBagLookup = Callable[[str], Optional[str]]


@dataclass
class ConfigSnapshot:
    equipment: dict[int, Equipment] = field(default_factory=dict)
    links: frozenset = frozenset()  # {(source_id, target_id)} - active links only
    bag_types: dict[int, BagType] = field(default_factory=dict)
    barcode_format: BarcodeFormatConfig = field(default_factory=BarcodeFormatConfig)
    mold_verification_enabled: bool = False

    def __post_init__(self) -> None:
        index: dict[str, list[Equipment]] = {}
        for eq in sorted(self.equipment.values(), key=lambda e: (not e.is_active, e.id)):
            index.setdefault(eq.barcode.strip().casefold(), []).append(eq)
        self._barcode_index = index

    def get(self, equipment_id: Optional[int]) -> Optional[Equipment]:
        if equipment_id is None:
            return None
        return self.equipment.get(equipment_id)

    def find_by_barcode(self, barcode: str) -> list[Equipment]:
        return list(self._barcode_index.get((barcode or "").strip().casefold(), []))

    def is_linked(self, source_id: Optional[int], target_id: Optional[int]) -> bool:
        return source_id is not None and target_id is not None and (source_id, target_id) in self.links

    def targets_of(self, source_id: Optional[int], category: Optional[EquipmentCategory] = None) -> list[Equipment]:
        result = []
        for src, tgt in self.links:
            if src != source_id:
                continue
            eq = self.equipment.get(tgt)
            if eq and (category is None or eq.category == category):
                result.append(eq)
        return sorted(result, key=lambda e: (e.sort_order, e.name))


class ValidationEngine:
    def __init__(self, snapshot: ConfigSnapshot):
        self.snapshot = snapshot
        self.parser = BagBarcodeParser(snapshot.barcode_format)

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #
    def stage_plan(self, bag_type: BagType) -> list[Stage]:
        plan = [Stage.BAG, Stage.HOPPER]
        if bag_type.silo_scan_required:
            plan.append(Stage.SILO)
        if bag_type.distribution_scan_required:
            plan.append(Stage.DISTRIBUTION_TANK)
        if self.snapshot.mold_verification_enabled and bag_type.mold_scan_required:
            plan.append(Stage.MOLD)
        return plan

    def expected_equipment(self, bag_type: BagType, stage: Stage) -> Optional[Equipment]:
        equipment_id = {
            Stage.HOPPER: bag_type.hopper_id,
            Stage.SILO: bag_type.silo_id,
            Stage.DISTRIBUTION_TANK: bag_type.distribution_tank_id,
            Stage.MOLD: bag_type.mold_id,
        }.get(stage)
        return self.snapshot.get(equipment_id)

    def destination(self, bag_type: BagType) -> Optional[Equipment]:
        """Tank whose quantity is increased by a loading: the final storage stage."""
        return self.snapshot.get(bag_type.distribution_tank_id) or self.snapshot.get(bag_type.silo_id)

    def initial_results(self, bag_type: BagType) -> dict[CheckItem, CheckResult]:
        plan = self.stage_plan(bag_type)
        results = {
            CheckItem.BAG: CheckResult.PENDING,
            CheckItem.COLOUR: CheckResult.PENDING,
            CheckItem.HOPPER: CheckResult.PENDING,
        }
        for stage in (Stage.SILO, Stage.DISTRIBUTION_TANK, Stage.MOLD):
            results[stage.check_item] = CheckResult.PENDING if stage in plan else CheckResult.NA
        return results

    @staticmethod
    def overall(results: Iterable[CheckResult] | dict) -> CheckResult:
        values = list(results.values()) if isinstance(results, dict) else list(results)
        if any(v == CheckResult.NG for v in values):
            return CheckResult.NG
        if any(v == CheckResult.PENDING for v in values):
            return CheckResult.PENDING
        return CheckResult.OK

    # ------------------------------------------------------------------ #
    # Configuration consistency
    # ------------------------------------------------------------------ #
    def check_bag_type(self, bag_type: BagType) -> list[str]:
        """Return configuration errors that prevent this bag type from being used."""
        snap = self.snapshot
        errors: list[str] = []
        if bag_type.weight_kg <= 0:
            errors.append("Bag weight must be greater than 0 kg.")
        rule_error = validate_rule(bag_type.match_type, bag_type.match_value, snap.barcode_format)
        if rule_error:
            errors.append(rule_error)

        hopper = snap.get(bag_type.hopper_id)
        if hopper is None or hopper.category != EquipmentCategory.HOPPER:
            errors.append("A hopper must be assigned.")
            hopper = None
        elif not hopper.is_active:
            errors.append(f"Hopper '{hopper.name}' is inactive.")

        silo = snap.get(bag_type.silo_id)
        if bag_type.silo_id is not None:
            if silo is None or silo.category != EquipmentCategory.SILO:
                errors.append("The assigned silo is not a valid silo.")
                silo = None
            elif not silo.is_active:
                errors.append(f"Silo '{silo.name}' is inactive.")
            elif hopper and not snap.is_linked(hopper.id, silo.id):
                errors.append(
                    f"Routing: {hopper.name} does not feed {silo.name}. "
                    "Add the link under Equipment > Routing or choose another silo."
                )
        if bag_type.silo_scan_required and bag_type.silo_id is None:
            errors.append("Silo scanning is required but no silo is assigned.")

        tank = snap.get(bag_type.distribution_tank_id)
        if bag_type.distribution_tank_id is not None:
            if tank is None or tank.category != EquipmentCategory.DISTRIBUTION_TANK:
                errors.append("The assigned distribution tank is not valid.")
            elif not tank.is_active:
                errors.append(f"Distribution tank '{tank.name}' is inactive.")
            elif hopper and not (
                snap.is_linked(hopper.id, tank.id) or (silo is not None and snap.is_linked(silo.id, tank.id))
            ):
                errors.append(
                    f"Routing: {tank.name} is not fed by {hopper.name}"
                    + (f" or {silo.name}" if silo else "")
                    + ". Add the link under Equipment > Routing or choose another tank."
                )
        if bag_type.distribution_scan_required and bag_type.distribution_tank_id is None:
            errors.append("Distribution tank scanning is required but no tank is assigned.")

        mold = snap.get(bag_type.mold_id)
        if bag_type.mold_id is not None:
            if mold is None or mold.category != EquipmentCategory.MOLD:
                errors.append("The assigned mold / machine is not valid.")
            elif not mold.is_active:
                errors.append(f"Mold / machine '{mold.name}' is inactive.")
            elif hopper and not snap.is_linked(hopper.id, mold.id):
                errors.append(f"Mold-Hopper compatibility: {hopper.name} does not feed {mold.name}.")
        elif bag_type.mold_scan_required and snap.mold_verification_enabled and hopper:
            if not [m for m in snap.targets_of(hopper.id, EquipmentCategory.MOLD) if m.is_active]:
                errors.append(f"Mold scanning is required but no active mold is linked to {hopper.name}.")
        return errors

    # ------------------------------------------------------------------ #
    # Identification
    # ------------------------------------------------------------------ #
    def identify_bag_types(self, barcode: str, exclude_id: Optional[int] = None) -> list[BagType]:
        parsed = self.parser.parse(barcode)
        if not parsed.format_ok:
            return []
        return [
            bt
            for bt in self.snapshot.bag_types.values()
            if bt.is_active and bt.id != exclude_id and bag_type_matches(bt, parsed)
        ]

    # ------------------------------------------------------------------ #
    # Scan validation
    # ------------------------------------------------------------------ #
    def validate_bag(
        self, bag_type: BagType, barcode: str, used_bag_lookup: Optional[UsedBagLookup] = None
    ) -> BagCheck:
        parsed = self.parser.parse(barcode)
        rule = describe_rule(bag_type)
        colour_expected = bag_type.colour + (f" ({bag_type.colour_code})" if bag_type.colour_code else "")
        weight = bag_type.weight_kg

        if not parsed.format_ok:
            bag = CheckOutcome(
                CheckItem.BAG, CheckResult.NG, rule, barcode,
                f"Bag barcode format not recognised: {parsed.format_error}.", NgReason.BARCODE_FORMAT,
            )
            colour = CheckOutcome(
                CheckItem.COLOUR, CheckResult.NG, colour_expected, "Unknown",
                "Colour cannot be verified (barcode format not recognised).", NgReason.BARCODE_FORMAT,
            )
            return BagCheck(bag, colour, parsed, None, weight)

        matched = bag_type_matches(bag_type, parsed)
        identified: Optional[BagType] = None
        if not matched:
            candidates = self.identify_bag_types(barcode, exclude_id=bag_type.id)
            identified = candidates[0] if candidates else None

        # ---- colour -----------------------------------------------------
        if parsed.colour:
            colour_ok = colour_matches(parsed.colour, bag_type)
            colour_scanned = parsed.colour
        elif matched:
            colour_ok = True
            colour_scanned = f"{bag_type.colour} (by bag type rule)"
        elif identified is not None:
            colour_ok = colours_equal(identified.colour, bag_type.colour)
            colour_scanned = identified.colour
        else:
            colour_ok = False
            colour_scanned = "Unknown"
        colour = CheckOutcome(
            CheckItem.COLOUR,
            CheckResult.OK if colour_ok else CheckResult.NG,
            colour_expected,
            colour_scanned,
            "Colour verified." if colour_ok else
            f"Colour mismatch: expected {bag_type.colour}, bag colour is {colour_scanned}.",
            NgReason.NONE if colour_ok else NgReason.COLOUR_MISMATCH,
        )

        # ---- bag type ---------------------------------------------------
        if matched:
            bag = CheckOutcome(CheckItem.BAG, CheckResult.OK, rule, barcode, "Bag type verified.")
        elif identified is not None:
            bag = CheckOutcome(
                CheckItem.BAG, CheckResult.NG, rule, barcode,
                f"Wrong bag: this barcode belongs to '{identified.name}', expected '{bag_type.name}'.",
                NgReason.BAG_MISMATCH,
            )
        else:
            bag = CheckOutcome(
                CheckItem.BAG, CheckResult.NG, rule, barcode,
                f"Barcode does not match bag type '{bag_type.name}' (rule: {rule}).",
                NgReason.BAG_MISMATCH,
            )

        if matched and self.snapshot.barcode_format.use_barcode_weight and parsed.weight_kg and parsed.weight_kg > 0:
            weight = parsed.weight_kg

        # ---- duplicate bag protection ------------------------------------
        if matched and colour_ok and used_bag_lookup is not None:
            previous = used_bag_lookup(parsed.raw)
            if previous:
                bag = CheckOutcome(
                    CheckItem.BAG, CheckResult.NG, rule, barcode,
                    f"Duplicate bag: this barcode was already loaded ({previous}).",
                    NgReason.DUPLICATE_BAG,
                )
        return BagCheck(bag, colour, parsed, identified, weight)

    def validate_equipment(self, stage: Stage, bag_type: BagType, barcode: str) -> CheckOutcome:
        category = stage.category
        if category is None:
            raise ValueError(f"{stage} is not an equipment stage")
        item = stage.check_item
        reason = STAGE_NG_REASON[stage]
        snap = self.snapshot
        expected = self.expected_equipment(bag_type, stage)
        any_linked_mold = stage == Stage.MOLD and bag_type.mold_id is None
        hopper = snap.get(bag_type.hopper_id)

        if any_linked_mold:
            expected_label = f"Any mold fed by {hopper.name}" if hopper else "Any linked mold"
        else:
            expected_label = expected.display if expected else "Not configured"

        def ng(message: str, ng_reason: NgReason = reason, scanned: str = barcode) -> CheckOutcome:
            return CheckOutcome(item, CheckResult.NG, expected_label, scanned, message, ng_reason)

        if expected is None and not any_linked_mold:
            return ng(f"No {category.label.lower()} is configured for bag type '{bag_type.name}'.",
                      NgReason.CONFIGURATION_ERROR)
        if expected is not None and not expected.is_active:
            return ng(f"Configured {category.label.lower()} '{expected.name}' is inactive.",
                      NgReason.CONFIGURATION_ERROR)

        matches = snap.find_by_barcode(barcode)
        in_category = [e for e in matches if e.category == category]
        if not in_category:
            if matches:
                other = matches[0]
                return ng(
                    f"Wrong equipment type: '{barcode}' is {other.name} ({other.category.label}). "
                    f"Scan the {category.label.lower()} barcode.",
                    scanned=f"{other.name} ({other.category.label})",
                )
            if self.identify_bag_types(barcode):
                return ng(f"A bag barcode was scanned. Scan the {category.label.lower()} barcode.",
                          scanned=f"Bag barcode {barcode}")
            return ng(f"Unknown barcode '{barcode}': not registered as a {category.label.lower()}.",
                      NgReason.UNKNOWN_BARCODE)

        scanned = in_category[0]
        if not scanned.is_active:
            return ng(f"{scanned.name} is inactive.", scanned=scanned.display)
        if any_linked_mold:
            if not snap.is_linked(bag_type.hopper_id, scanned.id):
                return ng(
                    f"Mold mismatch: {scanned.name} is not fed by {hopper.name if hopper else 'the hopper'}.",
                    scanned=scanned.display,
                )
        elif scanned.id != expected.id:
            return ng(
                f"Wrong {category.label.lower()}: expected {expected.name}, scanned {scanned.name}.",
                scanned=scanned.display,
            )
        return CheckOutcome(
            item, CheckResult.OK, expected_label, scanned.display, f"{scanned.name} verified.", equipment=scanned
        )
