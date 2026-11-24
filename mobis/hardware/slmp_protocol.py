"""Mitsubishi SLMP (MC protocol compatible) frame encoding and decoding.

Pure functions / classes without any I/O so they can be unit tested.

Supported:
* 3E and 4E frames, binary and ASCII data codes
* Batch read / write in word and bit units (commands 0x0401 / 0x1401)
* Read type name (0x0101) - used as link check / connection test
* Q/L series (3-byte device number, 1-byte device code; subcommands 0000/0001)
* iQ-R series (4-byte device number, 2-byte device code; subcommands 0002/0003)
* iQ-F series (Q-compatible frames, X/Y device numbers in octal)

Device numbering: X, Y, B, W, SB, SW, DX, DY are hexadecimal (octal X/Y on
iQ-F); all other supported devices are decimal.  File register ZR is
deliberately not supported because its numbering differs between series.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class SlmpError(Exception):
    """Base class of all SLMP errors."""


class SlmpAddressError(SlmpError, ValueError):
    """Invalid or unsupported device address."""


class SlmpProtocolError(SlmpError):
    """Malformed or unexpected response frame."""


class SlmpConnectionError(SlmpError):
    """Transport level failure (connect, timeout, connection closed)."""


END_CODE_HINTS = {
    0xC050: "ASCII data could not be converted to binary",
    0xC051: "number of points out of range",
    0xC052: "number of points out of range",
    0xC053: "number of points out of range",
    0xC054: "number of points out of range",
    0xC056: "device number out of range",
    0xC058: "request data length mismatch",
    0xC059: "command / subcommand not supported by this CPU",
    0xC05B: "device cannot be read / written",
    0xC05C: "request content error (device or points)",
    0xC05F: "request cannot be executed by the target CPU",
    0xC060: "request content error (bit device data)",
    0xC061: "request data length mismatch",
}


class SlmpEndCodeError(SlmpError):
    """The PLC answered with a non-zero end code (link is healthy)."""

    def __init__(self, end_code: int, command: int = 0):
        self.end_code = end_code
        self.command = command
        hint = END_CODE_HINTS.get(end_code, "")
        super().__init__(f"PLC error end code 0x{end_code:04X}" + (f" ({hint})" if hint else ""))


class PlcSeries(str, Enum):
    Q_L = "Q_L"
    IQ_R = "IQ_R"
    IQ_F = "IQ_F"


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    code: int        # binary device code
    ascii_q: str     # 2-character ASCII device code (Q/L/iQ-F)
    is_bit: bool
    base: int        # device number base (Q/L/iQ-R)


_DEVICE_LIST = [
    DeviceInfo("SM", 0x91, "SM", True, 10),
    DeviceInfo("SD", 0xA9, "SD", False, 10),
    DeviceInfo("X", 0x9C, "X*", True, 16),
    DeviceInfo("Y", 0x9D, "Y*", True, 16),
    DeviceInfo("M", 0x90, "M*", True, 10),
    DeviceInfo("L", 0x92, "L*", True, 10),
    DeviceInfo("F", 0x93, "F*", True, 10),
    DeviceInfo("V", 0x94, "V*", True, 10),
    DeviceInfo("B", 0xA0, "B*", True, 16),
    DeviceInfo("D", 0xA8, "D*", False, 10),
    DeviceInfo("W", 0xB4, "W*", False, 16),
    DeviceInfo("TS", 0xC1, "TS", True, 10),
    DeviceInfo("TC", 0xC0, "TC", True, 10),
    DeviceInfo("TN", 0xC2, "TN", False, 10),
    DeviceInfo("STS", 0xC7, "SS", True, 10),
    DeviceInfo("STC", 0xC6, "SC", True, 10),
    DeviceInfo("STN", 0xC8, "SN", False, 10),
    DeviceInfo("CS", 0xC4, "CS", True, 10),
    DeviceInfo("CC", 0xC3, "CC", True, 10),
    DeviceInfo("CN", 0xC5, "CN", False, 10),
    DeviceInfo("SB", 0xA1, "SB", True, 16),
    DeviceInfo("SW", 0xB5, "SW", False, 16),
    DeviceInfo("DX", 0xA2, "DX", True, 16),
    DeviceInfo("DY", 0xA3, "DY", True, 16),
    DeviceInfo("R", 0xAF, "R*", False, 10),
]
DEVICES = {d.name: d for d in _DEVICE_LIST}
_ALIASES = {"SS": "STS", "SC": "STC", "SN": "STN"}
SUPPORTED_DEVICES_TEXT = ", ".join(d.name for d in _DEVICE_LIST)


def _format_number(number: int, base: int) -> str:
    if base == 16:
        return f"{number:X}"
    if base == 8:
        return f"{number:o}"
    return str(number)


@dataclass(frozen=True)
class DeviceAddress:
    device: DeviceInfo
    number: int
    base: int

    @property
    def is_bit(self) -> bool:
        return self.device.is_bit

    def __str__(self) -> str:
        return f"{self.device.name}{_format_number(self.number, self.base)}"


def parse_device(text: str, series: PlcSeries | str = PlcSeries.Q_L) -> DeviceAddress:
    """Parse ``D100``, ``M20``, ``X1F``, ``W1A0``, ``SM400`` ... into a :class:`DeviceAddress`."""
    series = PlcSeries(series)
    raw = (text or "").strip().upper().replace(" ", "")
    if not raw:
        raise SlmpAddressError("Empty device address")
    limit = 0xFFFFFFFF if series == PlcSeries.IQ_R else 0xFFFFFF
    for length in (3, 2, 1):
        name, digits = raw[:length], raw[length:]
        name = _ALIASES.get(name, name)
        info = DEVICES.get(name)
        if info is None or not digits:
            continue
        base = 8 if (series == PlcSeries.IQ_F and info.name in ("X", "Y")) else info.base
        try:
            number = int(digits, base)
        except ValueError:
            continue
        if number > limit:
            raise SlmpAddressError(f"Device number out of range in '{text}'")
        return DeviceAddress(info, number, base)
    raise SlmpAddressError(
        f"Invalid or unsupported device address '{text}'. Supported devices: {SUPPORTED_DEVICES_TEXT}"
    )


def normalize_address(text: str, series: PlcSeries | str = PlcSeries.Q_L) -> str:
    """Canonical text form of an address (``d0100`` -> ``D100``); invalid input is upper-cased."""
    try:
        return str(parse_device(text, series))
    except SlmpAddressError:
        return (text or "").strip().upper()


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
CMD_BATCH_READ = 0x0401
CMD_BATCH_WRITE = 0x1401
CMD_READ_TYPE_NAME = 0x0101


@dataclass(frozen=True)
class FrameConfig:
    frame_type: str = "3E"        # 3E | 4E
    data_code: str = "BINARY"     # BINARY | ASCII
    series: PlcSeries = PlcSeries.Q_L
    network_no: int = 0
    pc_no: int = 0xFF
    module_io_no: int = 0x03FF
    station_no: int = 0
    monitoring_timer: int = 0x0010  # x 250 ms


class SlmpCodec:
    def __init__(self, config: FrameConfig):
        if config.frame_type not in ("3E", "4E"):
            raise ValueError(f"Unsupported frame type {config.frame_type}")
        if config.data_code not in ("BINARY", "ASCII"):
            raise ValueError(f"Unsupported data code {config.data_code}")
        self.config = config
        self.binary = config.data_code == "BINARY"
        self.e4 = config.frame_type == "4E"
        self.iqr = PlcSeries(config.series) == PlcSeries.IQ_R

    # ------------------------------------------------------------------ request parts
    def subcommand(self, bit_units: bool) -> int:
        if self.iqr:
            return 0x0003 if bit_units else 0x0002
        return 0x0001 if bit_units else 0x0000

    def encode_device(self, address: DeviceAddress) -> bytes:
        if self.binary:
            if self.iqr:
                return struct.pack("<IH", address.number, address.device.code)
            return struct.pack("<I", address.number)[:3] + bytes([address.device.code])
        if self.iqr:
            code = (address.device.name + "****")[:4]
            digits = _format_number(address.number, address.base).rjust(8, "0")
        else:
            code = address.device.ascii_q
            digits = _format_number(address.number, address.base).rjust(6, "0")
        return (code + digits).encode("ascii")

    def encode_count(self, count: int) -> bytes:
        if not 1 <= count <= 0xFFFF:
            raise ValueError("Point count out of range")
        return struct.pack("<H", count) if self.binary else f"{count:04X}".encode("ascii")

    def encode_words(self, values: Iterable[int]) -> bytes:
        values = [int(v) & 0xFFFF for v in values]
        if self.binary:
            return b"".join(struct.pack("<H", v) for v in values)
        return "".join(f"{v:04X}" for v in values).encode("ascii")

    def encode_bits(self, values: Iterable[int]) -> bytes:
        bits = [1 if int(v) else 0 for v in values]
        if not self.binary:
            return "".join(str(b) for b in bits).encode("ascii")
        out = bytearray()
        for i in range(0, len(bits), 2):
            high = bits[i]
            low = bits[i + 1] if i + 1 < len(bits) else 0
            out.append((high << 4) | low)
        return bytes(out)

    def read_request(self, address: DeviceAddress, count: int) -> tuple[int, int, bytes]:
        data = self.encode_device(address) + self.encode_count(count)
        return CMD_BATCH_READ, self.subcommand(address.is_bit), data

    def write_request(self, address: DeviceAddress, values: list[int]) -> tuple[int, int, bytes]:
        data = self.encode_device(address) + self.encode_count(len(values))
        data += self.encode_bits(values) if address.is_bit else self.encode_words(values)
        return CMD_BATCH_WRITE, self.subcommand(address.is_bit), data

    # ------------------------------------------------------------------ frame
    def build_frame(self, command: int, subcommand: int, request_data: bytes, serial: int = 0) -> bytes:
        c = self.config
        if self.binary:
            body = struct.pack("<HHH", c.monitoring_timer, command, subcommand) + request_data
            route = struct.pack("<BBHB", c.network_no, c.pc_no, c.module_io_no, c.station_no)
            head = struct.pack("<HHH", 0x0054, serial & 0xFFFF, 0) if self.e4 else struct.pack("<H", 0x0050)
            return head + route + struct.pack("<H", len(body)) + body
        body = f"{c.monitoring_timer:04X}{command:04X}{subcommand:04X}".encode("ascii") + request_data
        route = f"{c.network_no:02X}{c.pc_no:02X}{c.module_io_no:04X}{c.station_no:02X}".encode("ascii")
        head = (f"5400{serial & 0xFFFF:04X}0000" if self.e4 else "5000").encode("ascii")
        return head + route + f"{len(body):04X}".encode("ascii") + body

    @property
    def response_header_length(self) -> int:
        if self.binary:
            return 13 if self.e4 else 9
        return 26 if self.e4 else 18

    def parse_header(self, header: bytes) -> tuple[int, int]:
        """Return ``(response_data_length, serial_no)`` from a response header."""
        if len(header) < self.response_header_length:
            raise SlmpProtocolError("Response header too short")
        if self.binary:
            if self.e4:
                if header[0:2] != b"\xd4\x00":
                    raise SlmpProtocolError(f"Unexpected response subheader {header[0:2].hex()}")
                return struct.unpack_from("<H", header, 11)[0], struct.unpack_from("<H", header, 2)[0]
            if header[0:2] != b"\xd0\x00":
                raise SlmpProtocolError(f"Unexpected response subheader {header[0:2].hex()}")
            return struct.unpack_from("<H", header, 7)[0], 0
        text = header.decode("ascii", errors="replace")
        try:
            if self.e4:
                if text[0:4] != "D400":
                    raise SlmpProtocolError(f"Unexpected response subheader {text[0:4]!r}")
                return int(text[22:26], 16), int(text[4:8], 16)
            if text[0:4] != "D000":
                raise SlmpProtocolError(f"Unexpected response subheader {text[0:4]!r}")
            return int(text[14:18], 16), 0
        except ValueError as exc:
            raise SlmpProtocolError(f"Malformed ASCII header {text!r}") from exc

    def parse_body(self, body: bytes) -> tuple[int, bytes]:
        """Return ``(end_code, response_data)``."""
        if self.binary:
            if len(body) < 2:
                raise SlmpProtocolError("Response body too short")
            return struct.unpack_from("<H", body, 0)[0], body[2:]
        if len(body) < 4:
            raise SlmpProtocolError("Response body too short")
        try:
            return int(body[0:4].decode("ascii"), 16), body[4:]
        except ValueError as exc:
            raise SlmpProtocolError("Malformed end code") from exc

    # ------------------------------------------------------------------ response data
    def decode_words(self, data: bytes, count: int) -> list[int]:
        if self.binary:
            if len(data) < 2 * count:
                raise SlmpProtocolError("Word data too short")
            return list(struct.unpack(f"<{count}H", data[: 2 * count]))
        text = data.decode("ascii", errors="replace")
        if len(text) < 4 * count:
            raise SlmpProtocolError("Word data too short")
        try:
            return [int(text[i * 4:(i + 1) * 4], 16) for i in range(count)]
        except ValueError as exc:
            raise SlmpProtocolError("Malformed ASCII word data") from exc

    def decode_bits(self, data: bytes, count: int) -> list[int]:
        if self.binary:
            if len(data) < (count + 1) // 2:
                raise SlmpProtocolError("Bit data too short")
            out = []
            for i in range(count):
                byte = data[i // 2]
                nibble = (byte >> 4) if i % 2 == 0 else (byte & 0x0F)
                out.append(1 if nibble else 0)
            return out
        text = data.decode("ascii", errors="replace")
        if len(text) < count:
            raise SlmpProtocolError("Bit data too short")
        return [1 if ch == "1" else 0 for ch in text[:count]]

    def decode_type_name(self, data: bytes) -> tuple[str, int]:
        if self.binary:
            name = data[:16].decode("ascii", errors="replace").strip()
            code = struct.unpack_from("<H", data, 16)[0] if len(data) >= 18 else 0
            return name, code
        text = data.decode("ascii", errors="replace")
        code = int(text[16:20], 16) if len(text) >= 20 else 0
        return text[:16].strip(), code


def to_signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value
