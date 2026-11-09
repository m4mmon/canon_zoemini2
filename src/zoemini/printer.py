"""High-level API: printer state and printing (see protocol.md, print sequence)."""
from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

from .protocol import (
    Command, DeviceInfo, ErrorCode, Frame, QueueFlag, QueueStatus, SessionInfo,
    Settings, Status, build_command, print_ready_command,
)
from .transport import RfcommTransport

log = logging.getLogger(__name__)

DEFAULT_CHUNK = 650                    # chunk size when no MTU is known
MAX_CHUNK = 4096                       # cap: the Zoemini 2 announces an MTU of 0xFFFF
POLL_INTERVAL = 2.0                    # STATUS polling interval, in seconds
AUTO_POWER_OFF_CHOICES = (3, 5, 10)    # supported auto power-off delays, in minutes


class Transport(Protocol):
    def connect(self, timeout: float = ...) -> None: ...
    def close(self) -> None: ...
    def send(self, data: bytes) -> None: ...
    def recv_frame(self, timeout: float) -> Frame: ...


class PrinterError(Exception):
    """Error reported by the printer, or printing impossible."""


class PrinterNotReady(PrinterError):
    pass


# Progress events: (stage, percentage). Stages: "calibrating", "sending",
# "preparing", "printing", "done".
ProgressCallback = Callable[[str, int], None]


class Printer:
    def __init__(self, address: str, channel: int = 1, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.transport = transport or RfcommTransport(address, channel)
        self.session: SessionInfo | None = None
        self._sleep = sleep

    # --- Connection ------------------------------------------------------------

    def connect(self) -> SessionInfo:
        self.transport.connect()
        self.session = SessionInfo.from_frame(self._request(build_command(Command.START_SESSION)))
        return self.session

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "Printer":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- Requests --------------------------------------------------------------

    def _request(self, frame: bytes, timeout: float = 5.0) -> Frame:
        command = int.from_bytes(frame[5:7], "big")
        log.debug("> %s", frame.hex(" "))
        self.transport.send(frame)
        deadline = time.monotonic() + timeout
        while True:
            reply = self.transport.recv_frame(max(0.1, deadline - time.monotonic()))
            log.debug("< %s %s", reply.command.to_bytes(2, "big").hex(), reply.payload.hex(" "))
            if reply.command == command:
                return reply
            log.warning("unexpected response 0x%04X (expected 0x%04X), ignored", reply.command, command)

    def info(self) -> DeviceInfo:
        return DeviceInfo.from_frame(self._request(build_command(Command.GET_INFO)))

    def status(self) -> Status:
        return Status.from_frame(self._request(build_command(Command.STATUS)))

    def settings(self) -> Settings:
        return Settings.from_frame(self._request(build_command(Command.SETTING)))

    def set_auto_power_off(self, minutes: int) -> Settings:
        """Sets the auto power-off delay (3, 5 or 10 min). Returns the settings read back."""
        if minutes not in AUTO_POWER_OFF_CHOICES:
            raise ValueError(f"unsupported value: {minutes} (choices: 3, 5, 10)")
        reply = self._request(build_command(Command.SETTING, bytes([minutes]), write=True))
        if reply.status != ErrorCode.NO_ERROR:
            raise PrinterError(f"setting refused: {reply.error}")
        settings = Settings.from_frame(reply)
        if settings.auto_power_off != minutes:
            raise PrinterError(f"setting not applied: the printer reports {settings.auto_power_off} min")
        return settings

    # --- Printing --------------------------------------------------------------

    def wait_ready(self, timeout: float = 90.0, progress: ProgressCallback | None = None) -> Status:
        """Waits for a possible calibration to finish, then checks that the printer is ready."""
        deadline = time.monotonic() + timeout
        st = self.status()
        while QueueFlag.SCANNING_SMART_SHEET in st.queue_flags:
            if progress:
                progress("calibrating", 0)
            if time.monotonic() > deadline:
                raise PrinterNotReady("calibration (smart sheet) takes too long")
            self._sleep(POLL_INTERVAL)
            st = self.status()
        reason = st.blocking_reason()
        if reason:
            raise PrinterNotReady(reason)
        return st

    def print_jpeg(self, jpeg: bytes, progress: ProgressCallback | None = None,
                   timeout: float = 240.0) -> Status:
        """Prints an already prepared JPEG (see zoemini.image.prepare)."""
        if self.session is None:
            raise PrinterError("session not open: call connect()")
        self.wait_ready(progress=progress)

        ack = self._request(print_ready_command(len(jpeg)))
        if ack.status != ErrorCode.NO_ERROR:
            raise PrinterError(f"print refused: {ack.error}")

        mtu = self.session.mtu
        chunk = min(mtu, MAX_CHUNK) if mtu else DEFAULT_CHUNK
        for start in range(0, len(jpeg), chunk):
            self.transport.send(jpeg[start:start + chunk])
            if progress:
                progress("sending", min(100, (start + chunk) * 100 // len(jpeg)))

        done = self.transport.recv_frame(30.0)
        if done.command != Command.PRINT_RECEIVE_DATA_ACK:
            raise PrinterError(f"receive acknowledgement expected, got 0x{done.command:04X}")
        if done.status != ErrorCode.NO_ERROR:
            raise PrinterError(f"image refused: {done.error}")
        return self._follow_print(progress, timeout)

    def _follow_print(self, progress: ProgressCallback | None, timeout: float) -> Status:
        deadline = time.monotonic() + timeout
        started = False
        last: Status | None = None
        while time.monotonic() < deadline:
            self._sleep(POLL_INTERVAL)
            try:
                st = self.status()
            except (ConnectionError, TimeoutError):
                if last is not None and last.progress >= 100:
                    return last          # the printer drops the link after printing
                raise
            last = st
            if st.queue_status == QueueStatus.PAPER_JAM:
                raise PrinterError("paper jam")
            if st.error >= 0x21:
                raise PrinterError(f"error while printing: {ErrorCode.describe(st.error)}")
            if st.queue_status in (QueueStatus.PRINT_PREPARATION, QueueStatus.PRINT_PRINTING):
                started = True
                if progress:
                    stage = "preparing" if st.queue_status == QueueStatus.PRINT_PREPARATION else "printing"
                    progress(stage, max(0, st.progress))
            elif st.queue_status == QueueStatus.READY and (started or st.progress >= 100):
                if progress:
                    progress("done", 100)
                return st
            elif st.queue_status == QueueStatus.READY and time.monotonic() > deadline - timeout + 30:
                raise PrinterError("printing did not start (still READY after 30 s)")
            elif st.queue_status in (QueueStatus.TRANSIENT_ERROR, QueueStatus.RECOVERABLE_ERROR):
                log.warning("state %s, code %s", st.queue_status_name, ErrorCode.describe(st.error))
        raise PrinterError("timed out waiting for the end of printing")
