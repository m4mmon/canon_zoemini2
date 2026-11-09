"""Bluetooth RFCOMM transport (standard-library socket, Linux/BlueZ)."""
from __future__ import annotations

import logging
import socket
import time
from collections import deque

from .protocol import Frame, FrameReader

log = logging.getLogger(__name__)


class RfcommTransport:
    def __init__(self, address: str, channel: int = 1) -> None:
        self.address = address
        self.channel = channel
        self._sock: socket.socket | None = None
        self._reader = FrameReader()
        self._pending: deque[Frame] = deque()

    def connect(self, timeout: float = 10.0) -> None:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(timeout)
        try:
            sock.connect((self.address, self.channel))
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._reader = FrameReader()
        self._pending.clear()
        log.debug("connected to %s channel %d", self.address, self.channel)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def send(self, data: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("not connected")
        self._sock.settimeout(30)
        self._sock.sendall(data)

    def recv_frame(self, timeout: float) -> Frame:
        """Next complete response; TimeoutError if nothing arrives in time."""
        if self._sock is None:
            raise ConnectionError("not connected")
        deadline = time.monotonic() + timeout
        while not self._pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no response from the printer")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(1024)
            except socket.timeout as e:
                raise TimeoutError("no response from the printer") from e
            if not chunk:
                self.close()
                raise ConnectionError("connection closed by the printer")
            before = self._reader.discarded
            self._pending.extend(self._reader.feed(chunk))
            if self._reader.discarded != before:
                log.warning("%d out-of-frame byte(s) ignored", self._reader.discarded - before)
        return self._pending.popleft()
