"""Mitsubishi SLMP frames (byte level) and client round trips against a fake PLC."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from mobis.hardware.mitsubishi_slmp import SlmpClient
from mobis.hardware.slmp_protocol import (
    FrameConfig,
    PlcSeries,
    SlmpAddressError,
    SlmpCodec,
    SlmpConnectionError,
    SlmpEndCodeError,
    parse_device,
    to_signed16,
)


# --------------------------------------------------------------------------- #
# Device addresses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,name,number",
    [("D100", "D", 100), ("d0100", "D", 100), ("M0", "M", 0), ("X1F", "X", 0x1F), ("W1A0", "W", 0x1A0),
     ("SM400", "SM", 400), ("SD10", "SD", 10), ("B10", "B", 16), ("TN5", "TN", 5), ("STS3", "STS", 3),
     ("SS3", "STS", 3), ("DX10", "DX", 16), ("R20", "R", 20)],
)
def test_parse_device(text, name, number):
    address = parse_device(text)
    assert (address.device.name, address.number) == (name, number)


def test_iqf_uses_octal_for_x_and_y():
    assert parse_device("X17", PlcSeries.IQ_F).number == 0o17
    with pytest.raises(SlmpAddressError):
        parse_device("X18", PlcSeries.IQ_F)


@pytest.mark.parametrize("text", ["", "Q100", "D1A", "100", "ZR100", "D"])
def test_invalid_devices(text):
    with pytest.raises(SlmpAddressError):
        parse_device(text)


def test_to_signed16():
    assert to_signed16(0xFFFF) == -1 and to_signed16(32767) == 32767


# --------------------------------------------------------------------------- #
# Frame encoding (reference frames)
# --------------------------------------------------------------------------- #
def test_3e_binary_batch_read_words():
    codec = SlmpCodec(FrameConfig())
    cmd, sub, data = codec.read_request(parse_device("D100"), 3)
    frame = codec.build_frame(cmd, sub, data)
    assert frame.hex().upper() == "500000FFFF03000C00100001040000640000A80300"


def test_3e_binary_write_word_and_bits():
    codec = SlmpCodec(FrameConfig())
    cmd, sub, data = codec.write_request(parse_device("D100"), [0x1234])
    assert codec.build_frame(cmd, sub, data).hex().upper() == "500000FFFF03000E00100001140000640000A801003412"
    cmd, sub, data = codec.write_request(parse_device("M100"), [1, 0, 1])
    assert sub == 0x0001
    assert data[-2:] == bytes([0x10, 0x10])  # two points per byte, high nibble first


def test_3e_ascii_batch_read_words():
    codec = SlmpCodec(FrameConfig(data_code="ASCII"))
    cmd, sub, data = codec.read_request(parse_device("D100"), 3)
    assert codec.build_frame(cmd, sub, data).decode() == "500000FF03FF000018001004010000D*0001000003"


def test_iqr_binary_device_encoding_and_subcommand():
    codec = SlmpCodec(FrameConfig(series=PlcSeries.IQ_R))
    cmd, sub, data = codec.read_request(parse_device("D100", PlcSeries.IQ_R), 1)
    assert sub == 0x0002
    assert data == struct.pack("<IHH", 100, 0xA8, 1)


def test_4e_frame_carries_serial_number():
    codec = SlmpCodec(FrameConfig(frame_type="4E"))
    frame = codec.build_frame(0x0401, 0, b"", serial=0x1234)
    assert frame[:6] == bytes([0x54, 0x00, 0x34, 0x12, 0x00, 0x00])
    header = bytes([0xD4, 0x00, 0x34, 0x12, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0x03, 0x00, 0x04, 0x00])
    assert codec.parse_header(header) == (4, 0x1234)


def test_response_decoding():
    codec = SlmpCodec(FrameConfig())
    assert codec.decode_words(bytes([0x34, 0x12, 0xFF, 0xFF]), 2) == [0x1234, 0xFFFF]
    assert codec.decode_bits(bytes([0x10, 0x01]), 3) == [1, 0, 0]
    ascii_codec = SlmpCodec(FrameConfig(data_code="ASCII"))
    assert ascii_codec.decode_words(b"1234FFFF", 2) == [0x1234, 0xFFFF]
    assert ascii_codec.decode_bits(b"101", 3) == [1, 0, 1]
    assert codec.parse_body(bytes([0x59, 0xC0])) == (0xC059, b"")


# --------------------------------------------------------------------------- #
# Fake PLC (independent minimal SLMP server)
# --------------------------------------------------------------------------- #
class FakeSlmpServer:
    def __init__(self, data_code="BINARY", frame_type="3E", fail_device=None):
        self.binary = data_code == "BINARY"
        self.e4 = frame_type == "4E"
        self.fail_device = fail_device
        self.words: dict[tuple[int, int], int] = {}
        self.bits: dict[tuple[int, int], int] = {}
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self.sock.close()

    @staticmethod
    def _recv(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            while True:
                try:
                    self._handle(conn)
                except (ConnectionError, OSError):
                    return

    def _handle(self, conn):
        if self.binary:
            hlen = 13 if self.e4 else 9
            header = self._recv(conn, hlen)
            serial = struct.unpack_from("<H", header, 2)[0] if self.e4 else 0
            length = struct.unpack_from("<H", header, hlen - 2)[0]
            body = self._recv(conn, length)
            _timer, command, sub = struct.unpack_from("<HHH", body, 0)
            data = body[6:]
            number = int.from_bytes(data[0:3], "little")
            code = data[3] if len(data) > 3 else 0
            points = struct.unpack_from("<H", data, 4)[0] if len(data) >= 6 else 0
            payload = data[6:]
        else:
            hlen = 26 if self.e4 else 18
            header = self._recv(conn, hlen).decode()
            serial = int(header[4:8], 16) if self.e4 else 0
            length = int(header[hlen - 4:hlen], 16)
            body = self._recv(conn, length).decode()
            command, sub = int(body[4:8], 16), int(body[8:12], 16)
            data = body[12:]
            code = data[0:2]
            number = int(data[2:8], 16 if code in ("X*", "Y*", "W*", "B*") else 10) if data else 0
            points = int(data[8:12], 16) if len(data) >= 12 else 0
            payload = data[12:]

        response = b"" if self.binary else ""
        end_code = 0
        is_bit = sub == 0x0001
        if self.fail_device is not None and number == self.fail_device:
            end_code = 0xC056
        elif command == 0x0101:
            response = (b"Q03UDVCPU       " + struct.pack("<H", 0x0268)) if self.binary else "Q03UDVCPU       0268"
        elif command == 0x0401:
            if is_bit:
                values = [self.bits.get((str(code), number + i), 0) for i in range(points)]
                if self.binary:
                    out = bytearray()
                    for i in range(0, points, 2):
                        out.append((values[i] << 4) | (values[i + 1] if i + 1 < points else 0))
                    response = bytes(out)
                else:
                    response = "".join(str(v) for v in values)
            else:
                values = [self.words.get((str(code), number + i), 0) for i in range(points)]
                response = (b"".join(struct.pack("<H", v) for v in values) if self.binary
                            else "".join(f"{v:04X}" for v in values))
        elif command == 0x1401:
            for i in range(points):
                if is_bit:
                    if self.binary:
                        byte = payload[i // 2]
                        value = (byte >> 4) if i % 2 == 0 else (byte & 0x0F)
                    else:
                        value = int(payload[i])
                    self.bits[(str(code), number + i)] = 1 if value else 0
                elif self.binary:
                    self.words[(str(code), number + i)] = struct.unpack_from("<H", payload, i * 2)[0]
                else:
                    self.words[(str(code), number + i)] = int(payload[i * 4:(i + 1) * 4], 16)
        if self.binary:
            body = struct.pack("<H", end_code) + (response if end_code == 0 else b"\x00" * 9)
            head = (struct.pack("<HHH", 0x00D4, serial, 0) if self.e4 else struct.pack("<H", 0x00D0))
            conn.sendall(head + bytes([0, 0xFF]) + struct.pack("<HB", 0x03FF, 0) + struct.pack("<H", len(body)) + body)
        else:
            body = f"{end_code:04X}" + (response if end_code == 0 else "")
            head = f"D400{serial:04X}0000" if self.e4 else "D000"
            conn.sendall((head + "00FF03FF00" + f"{len(body):04X}" + body).encode())


@pytest.mark.parametrize("data_code,frame_type", [("BINARY", "3E"), ("ASCII", "3E"), ("BINARY", "4E"), ("ASCII", "4E")])
def test_client_round_trip(data_code, frame_type):
    server = FakeSlmpServer(data_code, frame_type)
    client = SlmpClient("127.0.0.1", server.port, FrameConfig(frame_type=frame_type, data_code=data_code), io_timeout=2)
    try:
        client.connect()
        assert client.read_type_name()[0] == "Q03UDVCPU"
        client.write_value("D200", 1234)
        client.write_value("D201", -2)
        assert client.read_words("D200", 2) == [1234, 0xFFFE]
        client.write_bits("M10", [1, 0, 1])
        assert client.read_bits("M10", 3) == [1, 0, 1]
        assert client.read_value("M12") == 1
    finally:
        client.close()
        server.close()


def test_end_code_error_keeps_connection():
    server = FakeSlmpServer(fail_device=999)
    client = SlmpClient("127.0.0.1", server.port, FrameConfig(), io_timeout=2)
    try:
        client.connect()
        with pytest.raises(SlmpEndCodeError) as info:
            client.read_value("D999")
        assert info.value.end_code == 0xC056
        assert client.connected
        client.write_value("D1", 7)
        assert client.read_value("D1") == 7
    finally:
        client.close()
        server.close()


def test_connection_refused_raises_connection_error():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    client = SlmpClient("127.0.0.1", port, FrameConfig(), connect_timeout=1)
    with pytest.raises(SlmpConnectionError):
        client.connect()


def test_response_timeout_closes_connection():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = SlmpClient("127.0.0.1", listener.getsockname()[1], FrameConfig(), io_timeout=0.3)
    try:
        client.connect()
        with pytest.raises(SlmpConnectionError):
            client.read_value("D0")
        assert not client.connected
    finally:
        client.close()
        listener.close()
