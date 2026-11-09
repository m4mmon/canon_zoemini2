"""Printer2 protocol (Canon Zoemini 2 / Ivy 2): frames, reassembly, decoding.

Pure module, no I/O. Reference: protocol.md.
"""
from __future__ import annotations

import enum
import struct
from dataclasses import dataclass

FRAME_LEN = 34
PAYLOAD_LEN = FRAME_LEN - 8
COMMAND_START = b"\x43\x0f"     # host → printer
RESPONSE_START = b"\x43\xf0"    # printer → host

DEVICE_TYPE = 0x0001
DEVICE_CODE = 0x20
SESSION_DEVICE_TYPE = 0xFFFF
SESSION_DEVICE_CODE = 0xFF


class Command(enum.IntEnum):
    START_SESSION = 0x0000
    STATUS = 0x0101
    GET_INFO = 0x0102
    SETTING = 0x0103
    PRINT_READY = 0x0301
    PRINT_RECEIVE_DATA_ACK = 0x0302   # sent by the printer only
    REBOOT = 0xFFFF


class ErrorCode(enum.IntEnum):
    NO_ERROR = 0x00
    BUSY = 0x11
    HIGH_TEMPERATURE = 0x12
    LOW_TEMPERATURE = 0x13
    BATTERY_TOO_LOW = 0x14
    OPEN_COVER = 0x15
    PAPER_EMPTY = 0x16
    WRONG_SMART_SHEET = 0x17
    PRINT_DATA_ERROR = 0x21
    PRINT_OPEN_COVER = 0x22
    PRINT_PAPER_EMPTY = 0x23
    PRINT_WRONG_SMART_SHEET = 0x24
    CAMERA_WRITE_ERROR = 0x25

    @classmethod
    def describe(cls, code: int) -> str:
        try:
            return cls(code).name
        except ValueError:
            return f"UNKNOWN_0x{code:02X}"


class QueueStatus(enum.IntEnum):
    READY = 0x00
    PRINT_PREPARATION = 0x01
    PRINT_PRINTING = 0x04
    PAPER_JAM = 0x10
    TRANSIENT_ERROR = 0xF0
    RECOVERABLE_ERROR = 0xF1


class QueueFlag(enum.IntFlag):
    COVER_OPEN = 1 << 0
    NO_PAPER = 1 << 1
    WRONG_PAPER = 1 << 2
    SCANNING_SMART_SHEET = 1 << 3
    SMART_SHEET_ERROR = 1 << 4


class ProtocolError(Exception):
    """Invalid or unexpected frame."""


# --- Frames -------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    device_type: int
    device_code: int
    command: int
    status: int        # command: 0 = GET, 1 = SET; response: error code
    payload: bytes     # 26 bytes

    @classmethod
    def parse(cls, data: bytes) -> "Frame":
        if len(data) != FRAME_LEN:
            raise ProtocolError(f"frame of {len(data)} bytes, {FRAME_LEN} expected")
        if data[:2] not in (COMMAND_START, RESPONSE_START):
            raise ProtocolError(f"unknown header {data[:2].hex()}")
        device_type, device_code, command, status = struct.unpack(">HBHB", data[2:8])
        return cls(device_type, device_code, command, status, bytes(data[8:]))

    @property
    def error(self) -> str:
        return ErrorCode.describe(self.status)


def build_command(command: int, payload: bytes = b"", *, write: bool = False) -> bytes:
    """34-byte command frame."""
    if len(payload) > PAYLOAD_LEN:
        raise ValueError(f"payload of {len(payload)} bytes, {PAYLOAD_LEN} at most")
    if command == Command.START_SESSION:
        dtype, dcode = SESSION_DEVICE_TYPE, SESSION_DEVICE_CODE
    else:
        dtype, dcode = DEVICE_TYPE, DEVICE_CODE
    head = COMMAND_START + struct.pack(">HBHB", dtype, dcode, command, 1 if write else 0)
    return (head + payload).ljust(FRAME_LEN, b"\x00")


def print_ready_command(jpeg_size: int) -> bytes:
    """PRINT_READY: JPEG size (uint32 BE) then 01 01."""
    return build_command(Command.PRINT_READY, struct.pack(">IBB", jpeg_size, 1, 1))


class FrameReader:
    """Reassembles the 34-byte responses from an RFCOMM stream.

    Resynchronises on the 0x43F0 header and counts the discarded bytes.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.discarded = 0

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames = []
        while True:
            i = self._buf.find(RESPONSE_START)
            if i < 0:
                keep = 1 if self._buf.endswith(RESPONSE_START[:1]) else 0
                self.discarded += len(self._buf) - keep
                del self._buf[:len(self._buf) - keep]
                return frames
            if i:
                self.discarded += i
                del self._buf[:i]
            if len(self._buf) < FRAME_LEN:
                return frames
            frames.append(Frame.parse(bytes(self._buf[:FRAME_LEN])))
            del self._buf[:FRAME_LEN]


# --- Response decoding ----------------------------------------------------------

@dataclass(frozen=True)
class PowerWord:
    """16-bit status word shared by START_SESSION and STATUS."""
    raw: int

    @property
    def battery(self) -> int:
        return self.raw & 0x7F

    @property
    def on_mains(self) -> bool:
        return bool(self.raw >> 7 & 1)

    @property
    def voltage_raw(self) -> int:
        """High byte: battery voltage, raw unit (assumption)."""
        return self.raw >> 8


@dataclass(frozen=True)
class SessionInfo:
    power: PowerWord
    mtu: int
    unknown8: int

    @classmethod
    def from_frame(cls, f: Frame) -> "SessionInfo":
        p = f.payload
        return cls(PowerWord(p[1] << 8 | p[2]), p[3] << 8 | p[4], p[0])


@dataclass(frozen=True)
class DeviceInfo:
    product_code: int
    mac: str
    serial: str
    model: str

    @classmethod
    def from_frame(cls, f: Frame) -> "DeviceInfo":
        p = f.payload
        mac = ":".join(f"{b:02X}" for b in p[0:6])
        n = p[6]
        serial = p[7:7 + n].decode("utf-8", "replace")
        m = p[7 + n] if 7 + n < len(p) else 0
        model = p[8 + n:8 + n + m].decode("utf-8", "replace")
        return cls(f.device_code, mac, serial, model)


@dataclass(frozen=True)
class Status:
    power: PowerWord
    error: int
    queue_status: int
    queue_flags: QueueFlag
    progress: int

    @classmethod
    def from_frame(cls, f: Frame) -> "Status":
        p = f.payload
        progress = struct.unpack(">b", p[6:7])[0]
        return cls(PowerWord(p[0] << 8 | p[1]), p[2], p[3],
                   QueueFlag(p[4] << 8 | p[5]), progress)

    @property
    def queue_status_name(self) -> str:
        try:
            return QueueStatus(self.queue_status).name
        except ValueError:
            return f"UNKNOWN_0x{self.queue_status:02X}"

    def blocking_reason(self) -> str | None:
        """Reason preventing a print, or None if the printer is ready."""
        flags = self.queue_flags
        if QueueFlag.COVER_OPEN in flags:
            return "cover open"
        if QueueFlag.NO_PAPER in flags:
            return "out of paper"
        if QueueFlag.SMART_SHEET_ERROR in flags:
            return "smart sheet error"
        if QueueFlag.WRONG_PAPER in flags:
            return "wrong paper"
        if self.error:
            return ErrorCode.describe(self.error)
        if self.queue_status != QueueStatus.READY:
            return f"printer busy ({self.queue_status_name})"
        return None


@dataclass(frozen=True)
class Settings:
    auto_power_off: int
    firmware: tuple[int, int, int]
    tmd_version: int
    photos_printed: int
    color_id: int
    unknown4: int
    unknown9_10: int

    @classmethod
    def from_frame(cls, f: Frame) -> "Settings":
        p = f.payload
        return cls(
            auto_power_off=p[0],
            firmware=(p[1], p[2], p[3]),
            tmd_version=p[5],
            photos_printed=p[6] << 8 | p[7],
            color_id=p[8],
            unknown4=p[4],
            unknown9_10=p[9] << 8 | p[10],
        )

    @property
    def firmware_str(self) -> str:
        return ".".join(map(str, self.firmware))

    @property
    def tmd_str(self) -> str:
        return f"0.0.{self.tmd_version}"
