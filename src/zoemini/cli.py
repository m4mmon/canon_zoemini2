"""Command-line tool: zoemini status | set | print | test-pattern | emulate."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from PIL import Image

from . import image
from .printer import Printer, PrinterError
from .protocol import ErrorCode

ENV_ADDRESS = "ZOEMINI_ADDRESS"


def _address(args) -> str:
    addr = args.address or os.environ.get(ENV_ADDRESS)
    if not addr:
        sys.exit(f"missing Bluetooth address: --address or variable {ENV_ADDRESS}")
    return addr


def cmd_status(args) -> int:
    with Printer(_address(args), args.channel) as p:
        s = p.session
        info, st, cfg = p.info(), p.status(), p.settings()
    print(f"Model         {info.model} (product code 0x{info.product_code:02X})")
    print(f"Serial no.    {info.serial}")
    print(f"MAC           {info.mac}")
    print(f"Firmware      {cfg.firmware_str}    TMD {cfg.tmd_str}")
    print(f"Photos        {cfg.photos_printed}")
    print(f"Auto off      {cfg.auto_power_off} min")
    print(f"Battery       {st.power.battery} %{' (mains)' if st.power.on_mains else ''}")
    print(f"State         {st.queue_status_name}, error {ErrorCode.describe(st.error)}")
    flags = [f.name for f in type(st.queue_flags) if f in st.queue_flags]
    print(f"Flags         {', '.join(flags) or 'none'}")
    reason = st.blocking_reason()
    print(f"Ready         {'yes' if reason is None else 'no: ' + reason}")
    if args.verbose:
        print(f"MTU {s.mtu}, raw voltage 0x{st.power.voltage_raw:02X}")
    return 0


def _print_jpeg(args, jpeg: bytes) -> int:
    if args.save_jpeg:
        Path(args.save_jpeg).write_bytes(jpeg)
        print(f"JPEG written: {args.save_jpeg} ({len(jpeg)} bytes)")
    if args.preview:
        image.preview(jpeg).save(args.preview)
        print(f"Preview written: {args.preview}")
    if args.dry_run:
        return 0

    last = [None]

    def progress(stage: str, pct: int) -> None:
        if (stage, pct) != last[0]:
            print(f"  {stage:<12} {pct:3d} %", flush=True)
            last[0] = (stage, pct)

    try:
        with Printer(_address(args), args.channel) as p:
            print(f"Connected. Sending {len(jpeg)} bytes…")
            p.print_jpeg(jpeg, progress)
    except PrinterError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1
    print("Printing done.")
    return 0


def cmd_print(args) -> int:
    with Image.open(args.image) as img:
        jpeg = image.prepare(img, "fit" if args.fit else "fill", not args.no_rotate, not args.raw)
    return _print_jpeg(args, jpeg)


def cmd_test_pattern(args) -> int:
    return _print_jpeg(args, image.prepare(image.test_pattern(), enhanced=not args.raw))


def cmd_set(args) -> int:
    with Printer(_address(args), args.channel) as p:
        before = p.settings().auto_power_off
        after = p.set_auto_power_off(args.auto_off).auto_power_off
    print(f"Auto power-off: {before} min → {after} min")
    return 0


def cmd_emulate(args) -> int:
    try:
        from . import bluez_emulator
    except ImportError:
        sys.exit("dbus-fast missing: pip install '.[emulator]'")
    from .emulator import make_serial
    serial = args.serial or make_serial(args.color)
    if len(serial) > 13:      # GET_INFO: 6 (MAC) + 1 + n + 1 + 5 ("PV223") ≤ 26 bytes
        sys.exit("serial number too long (13 characters at most)")
    return bluez_emulator.main(Path(args.out), args.adapter, args.visible, serial)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zoemini", description="Canon Zoemini 2 / Ivy 2")
    parser.add_argument("-a", "--address", help=f"Bluetooth address (or ${ENV_ADDRESS})")
    parser.add_argument("--channel", type=int, default=1, help="RFCOMM channel (1)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show exchanged frames")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="state, battery, settings").set_defaults(func=cmd_status)

    p_set = sub.add_parser("set", help="change a printer setting")
    p_set.add_argument("--auto-off", type=int, required=True, choices=(3, 5, 10), metavar="{3,5,10}",
                       help="auto power-off delay, in minutes")
    p_set.set_defaults(func=cmd_set)

    def output_opts(p):
        p.add_argument("--dry-run", action="store_true", help="prepare without printing")
        p.add_argument("--save-jpeg", metavar="FILE", help="save the JPEG that is sent")
        p.add_argument("--preview", metavar="FILE.png", help="upright preview")
        p.add_argument("--raw", action="store_true",
                       help="no tone curve and no sharpening (raw rendering)")

    p_print = sub.add_parser("print", help="print an image")
    p_print.add_argument("image")
    p_print.add_argument("--fit", action="store_true", help="whole image with white bands")
    p_print.add_argument("--no-rotate", action="store_true", help="do not rotate landscape images")
    output_opts(p_print)
    p_print.set_defaults(func=cmd_print)

    p_pattern = sub.add_parser("test-pattern", help="print the test pattern")
    output_opts(p_pattern)
    p_pattern.set_defaults(func=cmd_test_pattern)

    p_emul = sub.add_parser("emulate", help="pose as a Zoemini 2 (saves the received JPEGs)")
    p_emul.add_argument("--out", default="captures/emulator", help="folder for received JPEGs")
    p_emul.add_argument("--adapter", default="hci0")
    p_emul.add_argument("--visible", type=int, default=300, metavar="SECONDS",
                        help="Bluetooth discoverable duration (300)")
    p_emul.add_argument("--color", default="F", choices="ABCDEF",
                        help="announced colour (3rd character of the serial number): A black/pink/rose gold, "
                             "B pearl white, C blue, D rose gold/white, E pearl white/silver, "
                             "F navy/white (default)")
    p_emul.add_argument("--serial", help="full serial number (overrides --color)")
    p_emul.set_defaults(func=cmd_emulate)

    args = parser.parse_args(argv)
    if args.command == "emulate" and not args.verbose:
        logging.getLogger("zoemini").setLevel(logging.INFO)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (OSError, TimeoutError) as e:
        print(f"Connection error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
