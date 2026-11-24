"""Mitsubishi SLMP client over TCP or UDP (blocking; use from a worker thread)."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Union

from .slmp_protocol import (
    CMD_BATCH_READ,
    CMD_BATCH_WRITE,
    CMD_READ_TYPE_NAME,
    DeviceAddress,
    FrameConfig,
    SlmpCodec,
    SlmpConnectionError,
    SlmpEndCodeError,
    SlmpProtocolError,
    parse_device,
)

log = logging.getLogger("mobis.plc.slmp")

Address = Union[str, DeviceAddress]


class SlmpClient:
    def __init__(
        self,
        host: str,
        port: int,
        frame: FrameConfig,
        transport: str = "TCP",
        connect_timeout: float = 3.0,
        io_timeout: float = 3.0,
    ):
        self.host = host
        self.port = int(port)
        self.frame = frame
        self.transport = transport.upper()
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self.codec = SlmpCodec(frame)
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._serial = 0

    # ------------------------------------------------------------------ connection
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        self.close()
        try:
            if self.transport == "UDP":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(self.io_timeout)
                sock.connect((self.host, self.port))
            else:
                sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.io_timeout)
        except OSError as exc:
            raise SlmpConnectionError(f"Cannot connect to {self.host}:{self.port} ({self.transport}): {exc}") from exc
        self._sock = sock
        log.info("SLMP connected to %s:%s (%s %s %s)", self.host, self.port, self.transport,
                 self.frame.frame_type, self.frame.data_code)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ transport
    def _recv_exact(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise SlmpConnectionError("Connection closed by the PLC")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _transact(self, command: int, subcommand: int, data: bytes) -> bytes:
        with self._lock:
            if self._sock is None:
                raise SlmpConnectionError("Not connected")
            self._serial = (self._serial + 1) & 0xFFFF
            serial = self._serial
            frame = self.codec.build_frame(command, subcommand, data, serial)
            header_len = self.codec.response_header_length
            try:
                if self.transport == "UDP":
                    self._sock.send(frame)
                    packet = self._sock.recv(8192)
                    length, rx_serial = self.codec.parse_header(packet[:header_len])
                    body = packet[header_len:header_len + length]
                    if len(body) < length:
                        raise SlmpProtocolError("Truncated UDP response")
                else:
                    self._sock.sendall(frame)
                    length, rx_serial = self.codec.parse_header(self._recv_exact(header_len))
                    body = self._recv_exact(length)
            except TimeoutError as exc:
                self.close()
                raise SlmpConnectionError(f"No response within {self.io_timeout:g} s") from exc
            except SlmpConnectionError:
                self.close()
                raise
            except SlmpProtocolError:
                self.close()
                raise
            except OSError as exc:
                self.close()
                raise SlmpConnectionError(f"Communication error: {exc}") from exc
            if self.codec.e4 and rx_serial != serial:
                self.close()
                raise SlmpProtocolError(f"Serial number mismatch (sent {serial}, received {rx_serial})")
            end_code, payload = self.codec.parse_body(body)
            if end_code != 0:
                raise SlmpEndCodeError(end_code, command)
            return payload

    # ------------------------------------------------------------------ API
    def _address(self, address: Address) -> DeviceAddress:
        return address if isinstance(address, DeviceAddress) else parse_device(address, self.frame.series)

    def read_words(self, address: Address, count: int = 1) -> list[int]:
        """Batch read in word units (on a bit device each word holds 16 points)."""
        addr = self._address(address)
        data = self.codec.encode_device(addr) + self.codec.encode_count(count)
        payload = self._transact(CMD_BATCH_READ, self.codec.subcommand(False), data)
        return self.codec.decode_words(payload, count)

    def read_bits(self, address: Address, count: int = 1) -> list[int]:
        addr = self._address(address)
        data = self.codec.encode_device(addr) + self.codec.encode_count(count)
        payload = self._transact(CMD_BATCH_READ, self.codec.subcommand(True), data)
        return self.codec.decode_bits(payload, count)

    def write_words(self, address: Address, values: list[int]) -> None:
        addr = self._address(address)
        data = self.codec.encode_device(addr) + self.codec.encode_count(len(values)) + self.codec.encode_words(values)
        self._transact(CMD_BATCH_WRITE, self.codec.subcommand(False), data)

    def write_bits(self, address: Address, values: list[int]) -> None:
        addr = self._address(address)
        data = self.codec.encode_device(addr) + self.codec.encode_count(len(values)) + self.codec.encode_bits(values)
        self._transact(CMD_BATCH_WRITE, self.codec.subcommand(True), data)

    def read_value(self, address: Address) -> int:
        """Read one point: bit devices return 0/1, word devices an unsigned 16-bit value."""
        addr = self._address(address)
        return self.read_bits(addr, 1)[0] if addr.is_bit else self.read_words(addr, 1)[0]

    def write_value(self, address: Address, value: int) -> None:
        addr = self._address(address)
        if addr.is_bit:
            self.write_bits(addr, [1 if int(value) else 0])
        else:
            self.write_words(addr, [int(value)])

    def read_type_name(self) -> tuple[str, int]:
        payload = self._transact(CMD_READ_TYPE_NAME, 0x0000, b"")
        return self.codec.decode_type_name(payload)
