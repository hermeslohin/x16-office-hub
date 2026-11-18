"""BMX v1 (the Commander X16 bitmap format X16 Write uses for pictures) <-> Pillow images.

BMX v1, little endian: a 32-byte header
    0-2 "BMX", 3 version (1), 4 bits per pixel (1/2/4/8), 5 VERA colour depth (0..3),
    6-7 width, 8-9 height, 10 palette entries (0 = 256), 11 first palette index,
    12-13 offset of the pixel data, 14 compression (0 = none), 15 border colour, 16-31 reserved,
then the palette (2 bytes per entry, VERA format: byte0 = G<<4 | B, byte1 = R, 4 bits per channel)
for the indices [start, start + entries) (any other index is black), then `height` scanlines of
(width * bpp) / 8 bytes, the leftmost pixel in the most significant bits.

* bmx_to_image(data) -> RGB image; bmx_to_png(data) -> (PNG bytes, width, height)
* image_to_bmx(img) -> BMX v1 8 bpp (VERA depth 3), at most 320 x 240 (scaled down, never up),
  up to 256 colours from the 12-bit VERA colour space, transparent pixels white.
* picture_to_bmx(data, orient=False) -> BMX from untrusted PNG / JPEG / GIF / BMP bytes (pixel-capped);
  orient: turn it the way its EXIF orientation says (loose photos opened with conv 4; pictures inside
  Word documents are left as Word shows them).
* display_size(w, h): the size rule shared with X16 Write (f = min(1, 496/w, 640/h) in X16 page
  pixels, 80 of them per inch) -> EMU / twips helpers.
* rtf_group(bmx): the {\\*\\xwpbmx HEX} group X16 Write's RTF carries a picture in.

Every malformed or unreadable picture raises ConvError.
"""
from __future__ import annotations

import io
import struct

from PIL import Image, ImageOps

from .ooxml import ConvError

MAX_DIM = 1024                  # a BMX side, either way
X16_W, X16_H = 320, 240         # pictures sent to the X16 are scaled down to fit this
DOC_BUDGET = 160_000            # BMX bytes per document the hub sends to the X16
MAX_SRC_PIXELS = 16_000_000     # untrusted source pictures (decompression-bomb guard)
SRC_FORMATS = ["PNG", "JPEG", "GIF", "BMP"]
PAGE_W, PAGE_H = 496, 640       # X16 Write's picture box, in X16 page pixels
PX_PER_INCH = 80                # 512 px of text width = 6.4 in
EMU_PER_INCH, TWIPS_PER_INCH = 914400, 1440
_HDR = struct.Struct("<3sBBBHHBBHBB16s")


def _scale4(v: int) -> int:
    return (v * 15 + 127) // 255


def parse_header(data: bytes) -> dict:
    if len(data) < 32:
        raise ConvError("BMX: truncated header")
    magic, ver, bpp, depth, w, h, n, start, off, comp, border, _ = _HDR.unpack_from(data)
    if magic != b"BMX":
        raise ConvError("BMX: bad magic")
    if ver != 1:
        raise ConvError("BMX: unsupported version")
    if bpp not in (1, 2, 4, 8):
        raise ConvError("BMX: bad bits per pixel")
    if depth > 3:
        raise ConvError("BMX: bad colour depth")
    if comp != 0:
        raise ConvError("BMX: compressed data is not supported")
    if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM):
        raise ConvError("BMX: bad size")
    if (w * bpp) % 8:
        raise ConvError("BMX: a scanline is not a whole number of bytes")
    n = n or 256
    if start + n > 256:
        raise ConvError("BMX: palette runs past index 255")
    if off < 32 + 2 * n:
        raise ConvError("BMX: pixel data overlaps the palette")
    stride = (w * bpp) >> 3
    if len(data) < off + stride * h:
        raise ConvError("BMX: truncated pixel data")
    return {"bpp": bpp, "w": w, "h": h, "n": n, "start": start, "off": off, "stride": stride, "border": border}


def _decode(data: bytes) -> Image.Image:
    """BMX -> mode P image with a 256-entry palette (unused entries black)."""
    hd = parse_header(data)
    pal = bytearray(768)
    for i in range(hd["n"]):
        b0, b1 = data[32 + 2 * i], data[33 + 2 * i]
        k = 3 * (hd["start"] + i)
        pal[k:k + 3] = bytes(((b1 & 15) * 17, (b0 >> 4) * 17, (b0 & 15) * 17))
    raw = data[hd["off"]:hd["off"] + hd["stride"] * hd["h"]]
    rawmode = "P" if hd["bpp"] == 8 else f"P;{hd['bpp']}"
    img = Image.frombytes("P", (hd["w"], hd["h"]), raw, "raw", rawmode, hd["stride"], 1)
    img.putpalette(bytes(pal), "RGB")
    return img


def bmx_to_image(data: bytes) -> Image.Image:
    return _decode(data).convert("RGB")


def bmx_to_png(data: bytes) -> tuple[bytes, int, int]:
    img = _decode(data)
    f = scale_factor(img.width, img.height)
    buf = io.BytesIO()
    dpi = PX_PER_INCH / f
    img.save(buf, "PNG", optimize=True, dpi=(dpi, dpi))
    return buf.getvalue(), img.width, img.height


def scale_factor(w: int, h: int) -> float:
    return min(1.0, PAGE_W / w, PAGE_H / h)


def display_inches(w: int, h: int) -> tuple[float, float]:
    f = scale_factor(w, h)
    return w * f / PX_PER_INCH, h * f / PX_PER_INCH


def display_emu(w: int, h: int) -> tuple[int, int]:
    a, b = display_inches(w, h)
    return round(a * EMU_PER_INCH), round(b * EMU_PER_INCH)


def display_twips(w: int, h: int) -> tuple[int, int]:
    a, b = display_inches(w, h)
    return round(a * TWIPS_PER_INCH), round(b * TWIPS_PER_INCH)


def _flatten(img: Image.Image) -> Image.Image:
    """any mode -> RGB, scaled down to fit 320 x 240, transparent pixels white"""
    alpha = img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in img.info
    if img.mode.startswith("I;16"):                    # 16-bit greyscale PNG
        img = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    img = img.convert("RGBA" if alpha else "RGB")
    w, h = img.size
    if w > X16_W or h > X16_H:                         # scale first: the rest works on <= 76,800 pixels
        s = min(X16_W / w, X16_H / h)
        img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS, reducing_gap=3.0)
    if alpha:
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg
    return img.convert("RGB")


def image_to_bmx(img: Image.Image) -> bytes:
    img = _flatten(img)
    lut = [_scale4(v) * 17 for v in range(256)] * 3
    img12 = img.point(lut)
    colors = img12.getcolors(256)
    if colors is not None:                        # 256 colours or fewer: exact
        palette = [c for _, c in colors]
        src, dither = img12, Image.Dither.NONE
    else:
        mc = img12.quantize(256, method=Image.Quantize.MEDIANCUT)
        p = mc.getpalette() or []
        palette = list(dict.fromkeys(tuple(_scale4(v) * 17 for v in p[k:k + 3]) for k in range(0, len(p) - 2, 3)))
        src, dither = img, Image.Dither.FLOYDSTEINBERG
    palette = palette[:256]
    palimg = Image.new("P", (1, 1))
    palimg.putpalette(bytes(v for c in palette for v in c) + bytes(3 * (256 - len(palette))), "RGB")
    q = src.quantize(palette=palimg, dither=dither)
    pix = q.tobytes()
    n = max(pix) + 1 if pix else 1
    qp = (q.getpalette() or []) + [0] * 768
    pal = bytearray()
    for i in range(n):
        r, g, b = (_scale4(v) for v in qp[3 * i:3 * i + 3])
        pal += bytes(((g << 4) | b, r))
    w, h = q.size
    head = _HDR.pack(b"BMX", 1, 8, 3, w, h, n & 0xFF, 0, 32 + 2 * n, 0, 0, bytes(16))
    return head + bytes(pal) + pix


def picture_to_bmx(data: bytes, orient: bool = False) -> bytes:
    """untrusted PNG / JPEG / GIF / BMP bytes -> BMX (ConvError if unreadable or too big)"""
    try:
        img = Image.open(io.BytesIO(data), formats=SRC_FORMATS)
        if img.format == "JPEG":
            img.draft("RGB", (X16_W, X16_H))         # decode at 1/2 .. 1/8 scale when that's enough
        if img.width * img.height > MAX_SRC_PIXELS or img.width <= 0 or img.height <= 0:
            raise ConvError("picture is too large")
        img.load()
        if orient:
            img = ImageOps.exif_transpose(img)
        return image_to_bmx(img)
    except ConvError:
        raise
    except (MemoryError, Image.DecompressionBombError):
        raise ConvError("picture is too large") from None
    except Exception:                                  # Pillow raises many types on hostile data
        raise ConvError("picture could not be read") from None


def rtf_group(bmx: bytes) -> bytes:
    """the X16 Write picture group: lowercase hex, CRLF every 128 hex digits"""
    hx = bmx.hex().encode("ascii")
    return b"{\\*\\xwpbmx " + b"\r\n".join(hx[k:k + 128] for k in range(0, len(hx), 128)) + b"}"
