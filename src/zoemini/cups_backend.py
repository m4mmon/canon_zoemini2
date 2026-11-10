"""CUPS backend: sends a print job to a Zoemini 2 over Bluetooth (RFCOMM).

Device URI:  zoemini://XX-XX-XX-XX-XX-XX[?channel=1&wait=0]
  - the Bluetooth address, with "-" (or ":") separators;
  - channel: RFCOMM channel (1 by default);
  - wait: seconds to wait for the printer before giving the job back to CUPS
    for a later retry (0, the default, means wait as long as needed).

The job data is expected to be the output of the zoemini filter: one or more
concatenated 640 x 1608 JPEGs, one print each. Any other image Pillow can read
is prepared with the default settings (fill, rotation, enhancement), which
allows raw queues and manual tests.

Installation: copy the `zoemini-cups-backend` script of the venv to
/usr/lib/cups/backend/zoemini with mode 0755 (CUPS then runs it as the lp user;
no root access is needed to print).

Manual test, without CUPS:
  DEVICE_URI=zoemini://XX-XX-XX-XX-XX-XX zoemini-cups-backend 1 user title 1 "" photo.jpg
"""
from __future__ import annotations

import errno
import io
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from . import image
from .printer import Printer, PrinterError, PrinterNotReady

SCHEME = "zoemini"

# Backend exit codes (cups/backend.h)
CUPS_BACKEND_OK = 0
CUPS_BACKEND_FAILED = 1
CUPS_BACKEND_STOP = 4
CUPS_BACKEND_RETRY = 6

RETRY_DELAY = 10.0          # seconds between two attempts (printer off or not ready)

# Errors that waiting will not fix: no Bluetooth support, operation not permitted
FATAL_ERRNOS = {errno.EPERM, errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}

# Connection refused at authentication: the printer no longer knows this computer's
# link key ("PIN or key missing" -> EBADE, "authentication failure" -> EACCES).
# Waiting does not help, but pairing again does: the job keeps waiting meanwhile.
PAIRING_ERRNOS = {errno.EBADE, errno.EACCES}

# Printer-not-ready reasons (Status.blocking_reason) -> IPP printer-state-reasons
NOT_READY_STATES = {
    "cover open": "cover-open-report",
    "out of paper": "media-empty-error",
    "wrong paper": "media-needed-report",
    "smart sheet error": "media-needed-report",
}

STAGE_LABELS = {
    "calibrating": "Calibrating (blue sheet), please wait",
    "sending": "Sending",
    "preparing": "Preparing",
    "printing": "Printing",
    "done": "Done",
}

log = logging.getLogger(__name__)


def message(text: str) -> None:
    """One status line for cupsd (prefixes DEBUG:, INFO:, WARNING:, ERROR:, STATE:)."""
    print(text, file=sys.stderr, flush=True)


class _CupsLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname if record.levelname in ("DEBUG", "INFO", "WARNING", "ERROR") else "ERROR"
        return f"{level}: {record.name}: {record.getMessage()}"


class Reporter:
    """Keeps the printer-state-reasons and progress messages sent to cupsd."""

    def __init__(self) -> None:
        self._states: set[str] = set()
        self._last: tuple[str, int] | None = None

    def states(self, *keywords: str) -> None:
        """Sets exactly these state reasons (clears the others)."""
        wanted = set(keywords)
        for k in sorted(self._states - wanted):
            message(f"STATE: -{k}")
        for k in sorted(wanted - self._states):
            message(f"STATE: +{k}")
        self._states = wanted

    def progress(self, stage: str, pct: int) -> None:
        if (stage, pct) != self._last:
            label = STAGE_LABELS.get(stage, stage)
            # The printer reports no progress for calibration and the final state
            message(f"INFO: {label}" if stage in ("calibrating", "done") else f"INFO: {label} {pct} %")
            self._last = (stage, pct)


# --- Device URI and discovery ---------------------------------------------------

def parse_uri(uri: str) -> tuple[str, int, float]:
    """zoemini://XX-XX-XX-XX-XX-XX?channel=1&wait=0 -> (address, channel, wait)."""
    parts = urlsplit(uri)
    if parts.scheme != SCHEME:
        raise ValueError(f"not a {SCHEME}:// URI: {uri}")
    digits = re.sub(r"[-:]", "", parts.netloc)
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", digits):
        raise ValueError(f"invalid Bluetooth address in URI: {uri}")
    address = ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()
    query = parse_qs(parts.query)
    try:
        channel = int(query.get("channel", ["1"])[0])
        wait = float(query.get("wait", ["0"])[0])
    except ValueError as e:
        raise ValueError(f"invalid option in URI: {uri}") from e
    return address, channel, wait


def make_uri(address: str) -> str:
    return f"{SCHEME}://{address.upper().replace(':', '-')}"


def discover() -> None:
    """Lists the paired Zoemini printers (output format of `lpinfo -v`)."""
    try:
        out = subprocess.run(["bluetoothctl", "devices", "Paired"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.*Mini Printer.*)$", line.strip())
        if m:
            address, name = m.groups()
            print(f'direct {make_uri(address)} "Canon Zoemini 2" "{name}" '
                  f'"MFG:Canon;MDL:Zoemini 2;CMD:JPEG;"', flush=True)
    # Always offered, so that a URI can be entered by hand
    print(f'direct {SCHEME} "Unknown" "Canon Zoemini 2 (Bluetooth)"', flush=True)


# --- Job ------------------------------------------------------------------------

JPEG_BOUNDARY = b"\xff\xd9\xff\xd8"     # end of one JPEG, start of the next


def split_jpegs(data: bytes) -> list[bytes]:
    """Splits the filter's output (concatenated JPEGs, one per page and copy).

    In JPEG entropy-coded data every 0xFF is followed by 0x00 or a restart
    marker, so EOI immediately followed by SOI only occurs between two images
    (the filter's JPEGs carry no embedded thumbnail).
    """
    if not data.startswith(b"\xff\xd8"):
        return [data]
    parts = data.split(JPEG_BOUNDARY)
    if len(parts) == 1:
        return parts
    return ([parts[0] + b"\xff\xd9"]
            + [b"\xff\xd8" + p + b"\xff\xd9" for p in parts[1:-1]]
            + [b"\xff\xd8" + parts[-1]])


def to_printer_jpeg(data: bytes) -> bytes:
    """Returns the data unchanged if it is already a printer JPEG, else prepares it."""
    img = Image.open(io.BytesIO(data))          # UnidentifiedImageError (an OSError) if unknown
    if img.format == "JPEG" and img.size == image.PRINTOUT_SIZE:
        return data
    log.debug("job data is %s %dx%d: preparing it", img.format, *img.size)
    return image.prepare(img)


def print_once(address: str, channel: int, jpeg: bytes, wait: float, rep: Reporter) -> int:
    """Prints one copy, waiting for the printer to be switched on and ready."""
    started = time.monotonic()
    while True:
        sent = False

        def progress(stage: str, pct: int) -> None:
            nonlocal sent
            if stage == "sending":
                sent = True
            rep.progress(stage, pct)

        try:
            with Printer(address, channel) as p:
                rep.states()
                st = p.status()
                message(f"INFO: Connected, battery {st.power.battery} %")
                # From here on, a cancellation would leave the printer half-way:
                # finish the copy (the printer cannot be stopped anyway).
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                try:
                    p.print_jpeg(jpeg, progress)
                finally:
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
            rep.states()
            return CUPS_BACKEND_OK
        except PrinterNotReady as e:
            rep.states(NOT_READY_STATES.get(str(e), "other-warning"))
            message(f"INFO: Printer not ready: {e}")
        except PrinterError as e:
            message(f"ERROR: {e}")
            return CUPS_BACKEND_FAILED
        except OSError as e:                     # includes ConnectionError and TimeoutError
            if e.errno in FATAL_ERRNOS:
                message(f"ERROR: Bluetooth unavailable: {e}")
                return CUPS_BACKEND_STOP
            if sent:
                message(f"ERROR: connection lost during the job: {e}")
                return CUPS_BACKEND_FAILED
            if e.errno in PAIRING_ERRNOS:
                log.warning("pairing rejected by the printer: %s", e)
                rep.states("other-warning")
                message(f"INFO: Pairing lost: pair the printer again "
                        f"(bluetoothctl remove {address}, then pair and trust); the job is waiting")
            else:
                log.debug("connection failed: %s", e)
                rep.states("connecting-to-device")
                message("INFO: Waiting for the printer (switched off or out of range)")

        if wait and time.monotonic() - started > wait:
            message("INFO: Printer unavailable, the job will be retried later")
            return CUPS_BACKEND_RETRY
        time.sleep(RETRY_DELAY)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_CupsLogFormatter())
    logging.basicConfig(level=logging.DEBUG, handlers=[handler])

    if len(argv) == 1:
        discover()
        return CUPS_BACKEND_OK
    if len(argv) not in (6, 7):
        message(f"Usage: {argv[0]} job-id user title copies options [file]")
        return CUPS_BACKEND_FAILED

    try:
        address, channel, wait = parse_uri(os.environ.get("DEVICE_URI", argv[0]))
    except ValueError as e:
        message(f"ERROR: {e}")
        return CUPS_BACKEND_STOP

    # Copies are made here only for a file given on the command line
    # (raw job); otherwise the filters have already made them.
    if len(argv) == 7:
        data = Path(argv[6]).read_bytes()
        try:
            copies = max(1, int(argv[4]))
        except ValueError:
            copies = 1
    else:
        data = sys.stdin.buffer.read()
        copies = 1

    try:
        jpegs = [to_printer_jpeg(part) for part in split_jpegs(data)] * copies
    except (OSError, ValueError) as e:
        message(f"ERROR: unsupported job data: {e}")
        return CUPS_BACKEND_FAILED

    rep = Reporter()
    for n, jpeg in enumerate(jpegs, 1):
        if len(jpegs) > 1:
            message(f"INFO: Print {n} of {len(jpegs)}")
        rc = print_once(address, channel, jpeg, wait, rep)
        if rc != CUPS_BACKEND_OK:
            return rc
    message("INFO: Printed")
    return CUPS_BACKEND_OK


if __name__ == "__main__":
    sys.exit(main())
