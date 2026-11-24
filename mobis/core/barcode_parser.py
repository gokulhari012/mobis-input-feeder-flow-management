"""Barcode normalisation, bag barcode parsing and bag type identification rules.

The real bag barcode structure is not yet specified, so three configurable
parsing modes are supported:

* ``NONE``      - the barcode is opaque; bag types are identified by rules
                  (exact / prefix / contains / regular expression).
* ``DELIMITED`` - fields separated by a delimiter, field positions configurable.
* ``REGEX``     - regular expression with named groups ``material``, ``colour``,
                  ``lot``, ``serial`` and ``weight``.

All comparisons are case-insensitive: keyboard-wedge scanners invert letter
case when Caps Lock is active, which must not cause false NG results.
"""

from __future__ import annotations

import re
from typing import Optional

from .config_models import BarcodeFormatConfig, ScannerConfig
from .models import BagType, BarcodeMatchType, ParsedBarcode

_CONTROL_NAMES = {
    "NUL": 0, "SOH": 1, "STX": 2, "ETX": 3, "EOT": 4, "ENQ": 5, "ACK": 6,
    "BEL": 7, "BS": 8, "TAB": 9, "LF": 10, "VT": 11, "FF": 12, "CR": 13,
    "SO": 14, "SI": 15, "DLE": 16, "NAK": 21, "SYN": 22, "ETB": 23,
    "CAN": 24, "EM": 25, "SUB": 26, "ESC": 27, "FS": 28, "GS": 29,
    "RS": 30, "US": 31,
}
_TOKEN_RE = re.compile(r"<([A-Z]{2,3})>|\\x([0-9A-Fa-f]{2})|\\([rnt\\])")


def decode_escapes(text: str) -> str:
    """Translate ``<STX>``, ``\\x02``, ``\\r``, ``\\n``, ``\\t`` in configuration strings."""
    if not text:
        return ""

    def _replace(match: re.Match) -> str:
        if match.group(1):
            code = _CONTROL_NAMES.get(match.group(1))
            return chr(code) if code is not None else match.group(0)
        if match.group(2):
            return chr(int(match.group(2), 16))
        return {"r": "\r", "n": "\n", "t": "\t", "\\": "\\"}[match.group(3)]

    return _TOKEN_RE.sub(_replace, text)


# --------------------------------------------------------------------------- #
# Scanner data normalisation
# --------------------------------------------------------------------------- #
class BarcodeNormalizer:
    """Applies the scanner "data format" settings to raw scanner input."""

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.prefix = decode_escapes(config.prefix)
        self.suffix = decode_escapes(config.suffix)
        self.gs_replacement = decode_escapes(config.gs_replacement)

    def normalize(self, raw: Optional[str]) -> tuple[Optional[str], str]:
        """Return ``(barcode, "")`` or ``(None, reason)`` when the input is rejected."""
        if raw is None:
            return None, "Empty input"
        cfg = self.config
        text = raw.strip("\r\n")
        if self.prefix and text.startswith(self.prefix):
            text = text[len(self.prefix):]
        if self.suffix and text.endswith(self.suffix):
            text = text[: -len(self.suffix)]
        if self.gs_replacement:
            text = text.replace("\x1d", self.gs_replacement)
        if cfg.remove_control_chars:
            text = "".join(ch for ch in text if ch.isprintable())
        if cfg.strip_whitespace:
            text = text.strip()
        if cfg.uppercase:
            text = text.upper()
        if not text:
            return None, "Empty barcode"
        if len(text) < max(1, cfg.min_length):
            return None, f"Barcode too short ({len(text)} characters, minimum {cfg.min_length})"
        if cfg.max_length and len(text) > cfg.max_length:
            return None, f"Barcode too long ({len(text)} characters, maximum {cfg.max_length})"
        return text, ""


# --------------------------------------------------------------------------- #
# Bag barcode parsing
# --------------------------------------------------------------------------- #
class BagBarcodeParser:
    def __init__(self, fmt: BarcodeFormatConfig):
        self.fmt = fmt
        self._regex: Optional[re.Pattern] = None
        self._regex_error = ""
        if fmt.mode == "REGEX":
            if not fmt.regex:
                self._regex_error = "No regular expression configured"
            else:
                try:
                    self._regex = re.compile(fmt.regex, re.IGNORECASE)
                except re.error as exc:
                    self._regex_error = f"Invalid regular expression: {exc}"

    @property
    def error(self) -> str:
        """Configuration problem of the format ('' when usable)."""
        return self._regex_error

    @property
    def extracts_colour(self) -> bool:
        if self.fmt.mode == "DELIMITED":
            return self.fmt.field_colour > 0
        if self.fmt.mode == "REGEX" and self._regex is not None:
            return bool({"colour", "color"} & set(self._regex.groupindex))
        return False

    @property
    def extracts_material(self) -> bool:
        if self.fmt.mode == "DELIMITED":
            return self.fmt.field_material > 0
        if self.fmt.mode == "REGEX" and self._regex is not None:
            return "material" in self._regex.groupindex
        return False

    def parse(self, barcode: str) -> ParsedBarcode:
        mode = self.fmt.mode
        if mode == "DELIMITED":
            return self._parse_delimited(barcode)
        if mode == "REGEX":
            return self._parse_regex(barcode)
        return ParsedBarcode(raw=barcode)

    def _parse_delimited(self, barcode: str) -> ParsedBarcode:
        delimiter = decode_escapes(self.fmt.delimiter) or "|"
        parts = [p.strip() for p in barcode.split(delimiter)]
        positions = {
            "material": self.fmt.field_material,
            "colour": self.fmt.field_colour,
            "lot": self.fmt.field_lot,
            "serial": self.fmt.field_serial,
            "weight": self.fmt.field_weight,
        }
        needed = max(positions.values())
        if needed > len(parts):
            return ParsedBarcode(
                raw=barcode,
                format_ok=False,
                format_error=(
                    f"expected at least {needed} fields separated by '{delimiter}', "
                    f"found {len(parts)}"
                ),
            )

        def field(name: str) -> str:
            index = positions[name]
            return parts[index - 1] if index > 0 else ""

        return _build_parsed(
            barcode, field("material"), field("colour"), field("lot"), field("serial"), field("weight")
        )

    def _parse_regex(self, barcode: str) -> ParsedBarcode:
        if self._regex is None:
            return ParsedBarcode(raw=barcode, format_ok=False, format_error=self._regex_error)
        match = self._regex.fullmatch(barcode)
        if not match:
            return ParsedBarcode(
                raw=barcode, format_ok=False, format_error="barcode does not match the configured pattern"
            )
        groups = {k: (v or "") for k, v in match.groupdict().items()}
        return _build_parsed(
            barcode,
            groups.get("material", ""),
            groups.get("colour", "") or groups.get("color", ""),
            groups.get("lot", ""),
            groups.get("serial", ""),
            groups.get("weight", ""),
        )


def _build_parsed(raw: str, material: str, colour: str, lot: str, serial: str, weight_text: str) -> ParsedBarcode:
    weight: Optional[float] = None
    if weight_text:
        try:
            weight = float(weight_text.replace(",", "."))
        except ValueError:
            weight = None
    return ParsedBarcode(
        raw=raw, material_code=material, colour=colour, lot=lot, serial=serial, weight_kg=weight
    )


# --------------------------------------------------------------------------- #
# Bag type identification rules
# --------------------------------------------------------------------------- #
def _fold(text: str) -> str:
    return (text or "").strip().casefold()


def bag_type_matches(bag_type: BagType, parsed: ParsedBarcode) -> bool:
    """True when the scanned barcode satisfies the bag type identification rule."""
    value = (bag_type.match_value or "").strip()
    if not value:
        return False
    raw = parsed.raw
    match_type = bag_type.match_type
    if match_type == BarcodeMatchType.EXACT:
        return _fold(raw) == _fold(value)
    if match_type == BarcodeMatchType.PREFIX:
        return _fold(raw).startswith(_fold(value))
    if match_type == BarcodeMatchType.CONTAINS:
        return _fold(value) in _fold(raw)
    if match_type == BarcodeMatchType.REGEX:
        try:
            return re.fullmatch(value, raw, re.IGNORECASE) is not None
        except re.error:
            return False
    if match_type == BarcodeMatchType.MATERIAL_CODE:
        return bool(parsed.material_code) and _fold(parsed.material_code) == _fold(value)
    return False


def colour_matches(scanned_colour: str, bag_type: BagType) -> bool:
    """Compare a colour read from the barcode with the bag type colour / colour code."""
    scanned = _fold(scanned_colour)
    accepted = {_fold(bag_type.colour), _fold(bag_type.colour_code)} - {""}
    return bool(scanned) and scanned in accepted


def colours_equal(a: str, b: str) -> bool:
    return _fold(a) == _fold(b)


def describe_rule(bag_type: BagType) -> str:
    value = bag_type.match_value
    return {
        BarcodeMatchType.EXACT: f'equals "{value}"',
        BarcodeMatchType.PREFIX: f'starts with "{value}"',
        BarcodeMatchType.CONTAINS: f'contains "{value}"',
        BarcodeMatchType.REGEX: f"matches /{value}/",
        BarcodeMatchType.MATERIAL_CODE: f'material code "{value}"',
    }.get(bag_type.match_type, value)


def validate_rule(match_type: BarcodeMatchType, value: str, fmt: BarcodeFormatConfig) -> Optional[str]:
    """Return an error message when an identification rule cannot work."""
    if not (value or "").strip():
        return "Barcode identification value is required"
    if match_type == BarcodeMatchType.REGEX:
        try:
            re.compile(value)
        except re.error as exc:
            return f"Invalid regular expression: {exc}"
    if match_type == BarcodeMatchType.MATERIAL_CODE and not BagBarcodeParser(fmt).extracts_material:
        return (
            "Rule 'Material code field' requires a bag barcode format (DELIMITED or REGEX) "
            "that extracts the material field"
        )
    return None
