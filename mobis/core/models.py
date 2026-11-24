"""Domain models shared by the validation engine, repositories and UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Optional


# --------------------------------------------------------------------------- #
# Equipment
# --------------------------------------------------------------------------- #
class EquipmentCategory(str, Enum):
    HOPPER = "HOPPER"
    SILO = "SILO"
    DISTRIBUTION_TANK = "DISTRIBUTION_TANK"
    MOLD = "MOLD"

    @property
    def label(self) -> str:
        return _CATEGORY_LABELS[self]


_CATEGORY_LABELS = {
    EquipmentCategory.HOPPER: "Hopper",
    EquipmentCategory.SILO: "Silo",
    EquipmentCategory.DISTRIBUTION_TANK: "Distribution Tank",
    EquipmentCategory.MOLD: "Injection Mold / Machine",
}

#: Categories whose material quantity is monitored.
STORAGE_CATEGORIES = frozenset({EquipmentCategory.SILO, EquipmentCategory.DISTRIBUTION_TANK})


@dataclass(frozen=True)
class Equipment:
    id: int
    code: str
    name: str
    category: EquipmentCategory
    barcode: str
    description: str = ""
    capacity_kg: Optional[float] = None
    plc_code: Optional[int] = None
    ready_signal_address: str = ""
    ready_signal_ok_value: int = 1
    is_active: bool = True
    sort_order: int = 0

    @property
    def display(self) -> str:
        return f"{self.name} ({self.code})"


@dataclass(frozen=True)
class EquipmentLink:
    """Physical routing relationship: ``source`` feeds material into ``target``."""

    id: int
    source_id: int
    target_id: int
    is_active: bool = True
    description: str = ""


# --------------------------------------------------------------------------- #
# Bag types
# --------------------------------------------------------------------------- #
class BarcodeMatchType(str, Enum):
    EXACT = "EXACT"
    PREFIX = "PREFIX"
    CONTAINS = "CONTAINS"
    REGEX = "REGEX"
    MATERIAL_CODE = "MATERIAL_CODE"

    @property
    def label(self) -> str:
        return _MATCH_LABELS[self]


_MATCH_LABELS = {
    BarcodeMatchType.EXACT: "Exact match",
    BarcodeMatchType.PREFIX: "Starts with",
    BarcodeMatchType.CONTAINS: "Contains",
    BarcodeMatchType.REGEX: "Regular expression",
    BarcodeMatchType.MATERIAL_CODE: "Material code field",
}


@dataclass(frozen=True)
class BagType:
    id: int
    code: str
    name: str
    material_type: str
    colour: str
    colour_code: str = ""
    match_type: BarcodeMatchType = BarcodeMatchType.PREFIX
    match_value: str = ""
    weight_kg: float = 0.0
    hopper_id: Optional[int] = None
    silo_id: Optional[int] = None
    silo_scan_required: bool = False
    distribution_tank_id: Optional[int] = None
    distribution_scan_required: bool = False
    mold_id: Optional[int] = None
    mold_scan_required: bool = False
    description: str = ""
    is_active: bool = True

    @property
    def display(self) -> str:
        return f"{self.name}  [{self.code}]"


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
class Stage(str, Enum):
    """Scanning stages of a material loading transaction (in process order)."""

    BAG = "BAG"
    HOPPER = "HOPPER"
    SILO = "SILO"
    DISTRIBUTION_TANK = "DISTRIBUTION_TANK"
    MOLD = "MOLD"

    @property
    def label(self) -> str:
        return {
            Stage.BAG: "Bag",
            Stage.HOPPER: "Hopper",
            Stage.SILO: "Silo",
            Stage.DISTRIBUTION_TANK: "Distribution Tank",
            Stage.MOLD: "Mold / Machine",
        }[self]

    @property
    def category(self) -> Optional[EquipmentCategory]:
        return {
            Stage.HOPPER: EquipmentCategory.HOPPER,
            Stage.SILO: EquipmentCategory.SILO,
            Stage.DISTRIBUTION_TANK: EquipmentCategory.DISTRIBUTION_TANK,
            Stage.MOLD: EquipmentCategory.MOLD,
        }.get(self)

    @property
    def check_item(self) -> "CheckItem":
        return CheckItem(self.value)


class CheckItem(str, Enum):
    """Columns of the logic matrix (overall OK only when every item is OK/N/A)."""

    BAG = "BAG"
    COLOUR = "COLOUR"
    HOPPER = "HOPPER"
    SILO = "SILO"
    DISTRIBUTION_TANK = "DISTRIBUTION_TANK"
    MOLD = "MOLD"

    @property
    def label(self) -> str:
        return {
            CheckItem.BAG: "Bag Type",
            CheckItem.COLOUR: "Colour",
            CheckItem.HOPPER: "Hopper",
            CheckItem.SILO: "Silo",
            CheckItem.DISTRIBUTION_TANK: "Distribution Tank",
            CheckItem.MOLD: "Mold / Machine",
        }[self]


class CheckResult(str, Enum):
    PENDING = "PENDING"
    OK = "OK"
    NG = "NG"
    NA = "NA"

    @property
    def label(self) -> str:
        return {"PENDING": "PENDING", "OK": "OK", "NG": "NG", "NA": "N/A"}[self.value]


class NgReason(IntEnum):
    """Mismatch reason codes (optionally written to the PLC alarm code register)."""

    NONE = 0
    BAG_MISMATCH = 1
    COLOUR_MISMATCH = 2
    HOPPER_MISMATCH = 3
    SILO_MISMATCH = 4
    DISTRIBUTION_MISMATCH = 5
    MOLD_MISMATCH = 6
    DUPLICATE_BAG = 7
    UNKNOWN_BARCODE = 8
    CONFIGURATION_ERROR = 9
    BARCODE_FORMAT = 10


STAGE_NG_REASON = {
    Stage.BAG: NgReason.BAG_MISMATCH,
    Stage.HOPPER: NgReason.HOPPER_MISMATCH,
    Stage.SILO: NgReason.SILO_MISMATCH,
    Stage.DISTRIBUTION_TANK: NgReason.DISTRIBUTION_MISMATCH,
    Stage.MOLD: NgReason.MOLD_MISMATCH,
}


@dataclass(frozen=True)
class ParsedBarcode:
    raw: str
    material_code: str = ""
    colour: str = ""
    lot: str = ""
    serial: str = ""
    weight_kg: Optional[float] = None
    format_ok: bool = True
    format_error: str = ""


@dataclass(frozen=True)
class CheckOutcome:
    item: CheckItem
    result: CheckResult
    expected: str = ""
    scanned: str = ""
    message: str = ""
    ng_reason: NgReason = NgReason.NONE
    equipment: Optional[Equipment] = None

    @property
    def ok(self) -> bool:
        return self.result == CheckResult.OK


@dataclass(frozen=True)
class BagCheck:
    bag: CheckOutcome
    colour: CheckOutcome
    parsed: ParsedBarcode
    identified_bag_type: Optional[BagType]
    weight_kg: float

    @property
    def ok(self) -> bool:
        return self.bag.ok and self.colour.ok

    @property
    def failure(self) -> Optional[CheckOutcome]:
        """The outcome that should drive the operator message / alarm code."""
        if self.bag.result == CheckResult.NG:
            return self.bag
        if self.colour.result == CheckResult.NG:
            return self.colour
        return None


# --------------------------------------------------------------------------- #
# Persistence related enumerations
# --------------------------------------------------------------------------- #
class TransactionStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class ValidationStatus(str, Enum):
    PENDING = "PENDING"
    OK = "OK"
    NG = "NG"


class ScanResult(str, Enum):
    OK = "OK"
    NG = "NG"
    IGNORED = "IGNORED"


class InputSource(str, Enum):
    SCANNER_HID = "SCANNER_HID"
    SCANNER_SERIAL = "SCANNER_SERIAL"
    MANUAL = "MANUAL"
    SIMULATION = "SIMULATION"


class MesSyncStatus(str, Enum):
    PENDING = "PENDING"
    SYNCING = "SYNCING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


class InventoryChangeType(str, Enum):
    INITIAL = "INITIAL"
    LOADING = "LOADING"
    CORRECTION = "CORRECTION"


class Severity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
