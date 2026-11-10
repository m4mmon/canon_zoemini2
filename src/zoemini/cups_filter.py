"""CUPS filter: any image or PDF -> JPEG(s) in the Zoemini 2 format.

Called by CUPS as:  zoemini-cups-filter job-id user title copies options [file]
Writes to stdout one JPEG per page and per copy, concatenated
(MIME type application/vnd.zoemini-jpeg); the zoemini backend splits them.

Options (PPD, overridable with lp -o):
  ZoeminiScaling = Fill | Fit      fill the print (centred crop) or keep the whole image
  ZoeminiEnhance = On | Off        tone curve and sharpening (Off = zoemini print --raw)

PDF pages are rasterised with pdftoppm (poppler-utils).
"""
from __future__ import annotations

import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from . import image

PDF_RENDER_SIZE = 1800          # longest side of a rasterised PDF page, in pixels

DEFAULTS = {"ZoeminiScaling": "Fill", "ZoeminiEnhance": "On"}


def message(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def ppd_defaults(path: str | None) -> dict[str, str]:
    """*DefaultXxx values of the queue's PPD (set with lpadmin -o Xxx-default=...)."""
    values: dict[str, str] = {}
    if not path:
        return values
    try:
        text = Path(path).read_text(encoding="latin-1")
    except OSError:
        return values
    for m in re.finditer(r"^\*Default(Zoemini\w+):\s*(\S+)", text, re.M):
        values[m.group(1)] = m.group(2)
    return values


def job_options(options: str) -> dict[str, str]:
    """CUPS options string ("a=b c='d e' flag") -> dict."""
    result: dict[str, str] = {}
    try:
        words = shlex.split(options)
    except ValueError:
        words = options.split()
    for word in words:
        key, sep, value = word.partition("=")
        result[key] = value if sep else "true"
    return result


def settings(options: str) -> tuple[image.Mode, bool]:
    values = dict(DEFAULTS)
    values.update(ppd_defaults(os.environ.get("PPD")))
    values.update({k: v for k, v in job_options(options).items() if k in DEFAULTS})
    mode: image.Mode = "fit" if values["ZoeminiScaling"].lower() == "fit" else "fill"
    enhanced = values["ZoeminiEnhance"].lower() not in ("off", "false", "no")
    return mode, enhanced


def pdf_pages(data: bytes) -> list[Image.Image]:
    with tempfile.TemporaryDirectory(prefix="zoemini-") as tmp:
        src = Path(tmp, "job.pdf")
        src.write_bytes(data)
        subprocess.run(["pdftoppm", "-png", "-scale-to", str(PDF_RENDER_SIZE),
                        str(src), str(Path(tmp, "page"))], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        pages = []
        for p in sorted(Path(tmp).glob("page*.png")):
            with Image.open(p) as img:
                img.load()
                pages.append(img)
        return pages


def load_pages(data: bytes) -> list[Image.Image]:
    if data.startswith(b"%PDF"):
        return pdf_pages(data)
    img = Image.open(io.BytesIO(data))
    img.load()
    return [img]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if len(argv) not in (6, 7):
        message(f"Usage: {argv[0]} job-id user title copies options [file]")
        return 1
    try:
        copies = max(1, int(argv[4]))
    except ValueError:
        copies = 1
    mode, enhanced = settings(argv[5])
    data = Path(argv[6]).read_bytes() if len(argv) == 7 else sys.stdin.buffer.read()

    try:
        pages = load_pages(data)
    except subprocess.CalledProcessError as e:
        message(f"ERROR: pdftoppm failed: {e.stderr.decode(errors='replace').strip()}")
        return 1
    except FileNotFoundError:
        message("ERROR: pdftoppm not found (install poppler-utils)")
        return 1
    except OSError as e:
        message(f"ERROR: unsupported document: {e}")
        return 1
    if not pages:
        message("ERROR: empty document")
        return 1

    message(f"DEBUG: zoemini filter: {len(pages)} page(s), {copies} copies, "
            f"mode {mode}, enhancement {'on' if enhanced else 'off'}")
    jpegs = [image.prepare(p, mode=mode, enhanced=enhanced) for p in pages]
    out = sys.stdout.buffer
    for _ in range(copies):
        for n, jpeg in enumerate(jpegs, 1):
            message(f"PAGE: {n} 1")
            out.write(jpeg)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
