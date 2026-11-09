"""Emulated Zoemini 2 printer: answers like the real device and saves the received JPEGs.

Lets a client (the official app, or this package) "print" without a printer
and without paper; each received JPEG is written to disk. This module does not
depend on Bluetooth: `serve()` takes any connected socket (see
bluez_emulator.py for the BlueZ side).
"""
from __future__ import annotations

import datetime as dt
import logging
import socket
import struct
from pathlib import Path

from .protocol import COMMAND_START, FRAME_LEN, Command, ErrorCode

log = logging.getLogger(__name__)

RESPONSE_HEADER = bytes.fromhex("43f0") + struct.pack(">HB", 0x0001, 0x21)
POWER_WORD = 0x81E4                     # mains, battery 100 %

# Ivy 2 / Zoemini 2 colours: 3rd character of the serial number.
# The app picks the body icon from this letter; any other letter gives a generic icon.
COLOR_CODES = {
    "A": "black / pink / rose gold",
    "B": "pearl white",
    "C": "blue",
    "D": "rose gold and white",
    "E": "pearl white and silver",
    "F": "navy and white",
}


def make_serial(color: str = "F") -> str:
    """Dummy 12-character serial number for the given colour."""
    return f"EM{color}EMUL00001"


# Successive STATUS responses after an image is received (queueStatus, progress)
PRINT_TIMELINE = [(0x01, 25), (0x04, 25), (0x04, 50), (0x04, 75), (0x04, 100), (0x00, 100)]


def response(command: int, payload: bytes = b"", status: int = 0) -> bytes:
    head = RESPONSE_HEADER + struct.pack(">HB", command, status)
    return (head + payload).ljust(FRAME_LEN, b"\x00")


class FakeZoemini:
    def __init__(self, out_dir: Path, mac: str = "00:00:00:00:00:00",
                 serial: str | None = None) -> None:
        self.out_dir = Path(out_dir)
        self.mac = bytes.fromhex(mac.replace(":", ""))
        self.serial = (serial or make_serial()).encode()
        self.auto_power_off = 3
        self.photos = 0
        self._buf = bytearray()
        self._expected = 0              # image bytes still expected
        self._image = bytearray()
        self._timeline: list[tuple[int, int]] = []
        self.received: list[Path] = []

    # --- Responses -----------------------------------------------------------------

    def _status_payload(self) -> bytes:
        queue, progress = (self._timeline.pop(0) if self._timeline else (0x00, 0))
        return struct.pack(">HBBHb", POWER_WORD, 0, queue, 0, progress)

    def _info_payload(self) -> bytes:
        model = b"PV223"
        return self.mac + bytes([len(self.serial)]) + self.serial + bytes([len(model)]) + model

    def _setting_payload(self) -> bytes:
        return struct.pack(">B3sBBHBBB", self.auto_power_off, bytes([1, 4, 13]), 5, 24,
                           self.photos, 0, 0, 3)

    def _handle_command(self, frame: bytes) -> bytes:
        cmd = int.from_bytes(frame[5:7], "big")
        write = frame[7] == 1
        if cmd == Command.START_SESSION:
            return response(cmd, struct.pack(">BHH", 1, POWER_WORD, 0xFFFF))
        if cmd == Command.STATUS:
            return response(cmd, self._status_payload())
        if cmd == Command.GET_INFO:
            return response(cmd, self._info_payload())
        if cmd == Command.SETTING:
            if write:
                self.auto_power_off = frame[8]
                log.info("setting written: auto power-off %d min", frame[8])
            return response(cmd, self._setting_payload())
        if cmd == Command.PRINT_READY:
            self._expected = int.from_bytes(frame[8:12], "big")
            self._image.clear()
            log.info("PRINT_READY: %d bytes announced, bytes 12-13 = %s",
                     self._expected, frame[12:14].hex(" "))
            return response(cmd, self._status_payload())
        if cmd >> 8 == 0x02:
            log.warning("firmware command 0x%04X REFUSED", cmd)
            return response(cmd, status=ErrorCode.BUSY)
        log.warning("unknown command 0x%04X: %s", cmd, frame.hex(" "))
        return response(cmd, status=ErrorCode.BUSY)

    def _finish_image(self) -> bytes:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"app_{dt.datetime.now():%Y%m%d_%H%M%S}.jpg"
        path.write_bytes(self._image)
        self.received.append(path)
        self.photos += 1
        self._timeline = list(PRINT_TIMELINE)
        log.info("image received: %s (%d bytes)", path, len(self._image))
        return response(Command.PRINT_RECEIVE_DATA_ACK, self._status_payload_idle())

    def _status_payload_idle(self) -> bytes:
        return struct.pack(">HBBHb", POWER_WORD, 0, 0, 0, 0)

    # --- Stream ---------------------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Received data → responses to send back."""
        out = bytearray()
        self._buf.extend(data)
        while self._buf:
            if self._expected:
                take = min(self._expected, len(self._buf))
                self._image += self._buf[:take]
                del self._buf[:take]
                self._expected -= take
                if not self._expected:
                    out += self._finish_image()
                continue
            i = self._buf.find(COMMAND_START)
            if i < 0:
                log.warning("out-of-frame bytes ignored: %s", bytes(self._buf).hex(" "))
                self._buf.clear()
                break
            if i:
                log.warning("out-of-frame bytes ignored: %s", bytes(self._buf[:i]).hex(" "))
                del self._buf[:i]
            if len(self._buf) < FRAME_LEN:
                break
            frame = bytes(self._buf[:FRAME_LEN])
            del self._buf[:FRAME_LEN]
            log.debug("> %s", frame.hex(" "))
            reply = self._handle_command(frame)
            log.debug("< %s", reply.hex(" "))
            out += reply
        return bytes(out)

    def serve(self, sock: socket.socket) -> None:
        """Talks to a connected client until it disconnects."""
        with sock:
            while True:
                try:
                    data = sock.recv(4096)
                except OSError as e:
                    log.info("connection ended: %s", e)
                    return
                if not data:
                    log.info("client disconnected")
                    return
                reply = self.feed(data)
                if reply:
                    sock.sendall(reply)
