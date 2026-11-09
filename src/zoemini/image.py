"""Image preparation for the PV_223 family (Ivy 2 / Zoemini 2).

Format described in protocol.md (image format): 634 × 1596 content area,
non-square pixels (ratio 1.675), placed in 640 × 1608, rotated by 180°, edges
filled, baseline JPEG 4:4:4 at quality 90.

Rendering (optional, on by default): adaptive tone curve (gamma depending on
mean brightness) and sharpening, to compensate the ZINK paper's compression
of shadows and highlights. No dithering.
"""
from __future__ import annotations

import io
from typing import Literal

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat

PRINTOUT_SIZE = (640, 1608)
CONTENT_SIZE = (634, 1596)
PIXEL_RATIO = 1608 * 2 / (640 * 3)          # 1.675: vertical / horizontal resolution
SHIFT_OFFSET = 4
JPEG_QUALITY = 90

# Physical width / height ratio of the content area (≈ 2:3)
CONTENT_ASPECT = CONTENT_SIZE[0] / (CONTENT_SIZE[1] / PIXEL_RATIO)

Mode = Literal["fill", "fit"]

# Rendering parameters
GAMMA_BASE, GAMMA_SLOPE = 0.72, 0.0012       # gamma = base + slope × mean brightness
GAMMA_MIN, GAMMA_MAX = 0.75, 0.95
SHARPEN = dict(radius=2.0, percent=50, threshold=2)


def tone_gamma(img: Image.Image) -> float:
    """Applied gamma: the darker the image, the more the shadows are lifted."""
    mean = ImageStat.Stat(img.convert("L")).mean[0]
    return min(GAMMA_MAX, max(GAMMA_MIN, GAMMA_BASE + GAMMA_SLOPE * mean))


def enhance(img: Image.Image) -> Image.Image:
    """Adaptive tone curve then sharpening (on the content-size image)."""
    g = tone_gamma(img)
    lut = [round(255 * (i / 255) ** g) for i in range(256)] * 3
    return img.point(lut).filter(ImageFilter.UnsharpMask(**SHARPEN))


def _to_portrait(img: Image.Image, auto_rotate: bool) -> Image.Image:
    if auto_rotate and img.width > img.height:
        return img.rotate(90, expand=True)
    return img


def _crop_or_pad(img: Image.Image, mode: Mode) -> Image.Image:
    """Brings the image to the physical ratio of the content area."""
    w, h = img.size
    if mode == "fill":
        if w / h > CONTENT_ASPECT:                       # too wide: crop the sides
            nw = round(h * CONTENT_ASPECT)
            left = (w - nw) // 2
            return img.crop((left, 0, left + nw, h))
        nh = round(w / CONTENT_ASPECT)                   # too tall: crop top and bottom
        top = (h - nh) // 2
        return img.crop((0, top, w, top + nh))
    # fit: white bands
    if w / h > CONTENT_ASPECT:
        nw, nh = w, round(w / CONTENT_ASPECT)
    else:
        nw, nh = round(h * CONTENT_ASPECT), h
    canvas = Image.new("RGB", (nw, nh), "white")
    canvas.paste(img, ((nw - w) // 2, (nh - h) // 2))
    return canvas


def _fill_edges(img: Image.Image) -> Image.Image:
    """4-row shift and repetition of the edge rows (protocol.md, edge filling)."""
    w, h = PRINTOUT_SIZE
    top = (h - CONTENT_SIZE[1]) // 2                     # 6
    bottom = (h + CONTENT_SIZE[1]) // 2 - 1              # 1601
    first_row = img.crop((0, top, w, top + 1))
    last_row = img.crop((0, bottom, w, bottom + 1))
    out = img.copy()
    out.paste(img.crop((0, 0, w, h - SHIFT_OFFSET)), (0, SHIFT_OFFSET))
    for y in range((w - CONTENT_SIZE[0]) + SHIFT_OFFSET):     # 10 rows
        out.paste(first_row, (0, y))
    for y in range(h - SHIFT_OFFSET, h):
        out.paste(last_row, (0, y))
    return out


def render(img: Image.Image, mode: Mode = "fill", auto_rotate: bool = True,
           enhanced: bool = True) -> Image.Image:
    """640 × 1608 image ready to encode, as the printer expects it."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = _crop_or_pad(_to_portrait(img, auto_rotate), mode)
    img = img.resize(CONTENT_SIZE, Image.Resampling.LANCZOS)
    if enhanced:
        img = enhance(img)
    canvas = Image.new("RGB", PRINTOUT_SIZE, "white")
    canvas.paste(img, ((PRINTOUT_SIZE[0] - CONTENT_SIZE[0]) // 2,
                       (PRINTOUT_SIZE[1] - CONTENT_SIZE[1]) // 2))
    return _fill_edges(canvas.rotate(180))


def encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, subsampling=0, progressive=False)
    return buf.getvalue()


def prepare(img: Image.Image, mode: Mode = "fill", auto_rotate: bool = True,
            enhanced: bool = True) -> bytes:
    """Any image → JPEG to send to the printer."""
    return encode(render(img, mode, auto_rotate, enhanced))


def preview(jpeg: bytes) -> Image.Image:
    """Upright preview, at physical proportions, of a prepared JPEG."""
    img = Image.open(io.BytesIO(jpeg)).rotate(180)
    return img.resize((img.width, round(img.height / PIXEL_RATIO)))


def test_pattern() -> Image.Image:
    """2:3 test pattern: grid, circle, edge and corner marks, colour bars."""
    w, h = 1200, 1800
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for x in range(0, w + 1, 100):
        d.line([(x, 0), (x, h)], fill=(170, 170, 170), width=2)
    for y in range(0, h + 1, 100):
        d.line([(0, y), (w, y)], fill=(170, 170, 170), width=2)
    d.rectangle([0, 0, w - 1, h - 1], outline="black", width=6)
    d.rectangle([30, 30, w - 31, h - 31], outline="red", width=4)         # mark at ~1.3 mm
    d.ellipse([200, 300, 1000, 1100], outline="blue", width=10)          # must print round
    d.line([(600, 300), (600, 1100)], fill="blue", width=4)
    d.line([(200, 700), (1000, 700)], fill="blue", width=4)
    colors = ["#00ffff", "#ff00ff", "#ffff00", "#000000", "#ff0000", "#00ff00", "#0000ff", "#808080"]
    bw = (w - 200) // len(colors)
    for i, c in enumerate(colors):
        d.rectangle([100 + i * bw, 1250, 100 + (i + 1) * bw, 1450], fill=c)
    for i in range(16):                                                  # grey ramp
        g = round(i * 255 / 15)
        d.rectangle([100 + i * 62, 1500, 100 + (i + 1) * 62, 1600], fill=(g, g, g))
    font_size = 60
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        font = None
    d.text((60, 60), "TOP", fill="black", font=font)
    d.text((60, h - 140), "BOTTOM", fill="black", font=font)
    d.text((w - 220, 60), "RIGHT", fill="black", font=font)
    d.polygon([(600, 120), (560, 200), (640, 200)], fill="black")        # arrow pointing up
    return img
