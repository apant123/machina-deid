"""Neutral replacements for embedded preview images, byte-for-byte the same size.

Native CAD files store previews (SolidWorks `Preview`/`PreviewPNG`, OLE thumbnails, Creo, ...)
inside length-coupled containers. Drawing previews show the title block (names, logos), so every
preview is replaced by a flat grey image of the same dimensions, padded to the exact original byte
length with an ancillary chunk (PNG), a comment segment (JPEG) or by keeping the header and
filling the pixel array (BMP / DIB). The image stays valid; it just shows nothing.
"""
from __future__ import annotations

import io
import struct
import zlib

GREY = 0xC8


def _png_chunk(typ: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


def _noise(n: int) -> bytes:
    import hashlib
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(b"deid-pad-%d" % i).digest()
        i += 1
    return bytes(out[:n])


def neutral_png(orig: bytes, noise: int = 0) -> bytes | None:
    """noise: leading pseudo-random bytes in the padding chunk (lets a caller tune the compressed size)."""
    if not orig.startswith(b"\x89PNG\r\n\x1a\n") or len(orig) < 33:
        return None
    w, h = struct.unpack(">II", orig[16:24])
    if not (0 < w <= 20000 and 0 < h <= 20000):
        return None
    row = b"\x00" + b"\x00" * ((w + 7) // 8)  # 1-bit palette, index 0 = grey (compresses to ~nothing)
    raw = row * h
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 1, 3, 0, 0, 0))
    plte = _png_chunk(b"PLTE", bytes([GREY, GREY, GREY]))
    idat = _png_chunk(b"IDAT", zlib.compress(raw, 9))
    end = _png_chunk(b"IEND", b"")
    base = orig[:8] + ihdr + plte + idat
    gap = len(orig) - len(base) - len(end)
    if gap < 0:
        return None
    if gap == 0:
        return base + end
    if gap < 12:
        return None
    n = gap - 12
    k = min(max(noise, 0), n)
    return base + _png_chunk(b"deID", _noise(k) + b"\x00" * (n - k)) + end  # ancillary, private, not copy-safe


def neutral_bmp(orig: bytes) -> bytes | None:
    """'BM' file or a bare DIB (BITMAPINFOHEADER): keep headers + palette, grey the pixel array."""
    if orig[:2] == b"BM" and len(orig) >= 54:
        off = struct.unpack_from("<I", orig, 10)[0]
        if 14 <= off <= len(orig):
            return orig[:off] + bytes([_dominant(orig[off:])]) * (len(orig) - off)
        return None
    for base in (0, 4):  # bare DIB, or u32 length + DIB (SolidWorks 'Preview')
        if len(orig) >= base + 40 and struct.unpack_from("<I", orig, base)[0] in (40, 108, 124) and \
                (base == 0 or struct.unpack_from("<I", orig, 0)[0] in (len(orig) - 4, len(orig))):
            hs = struct.unpack_from("<I", orig, base)[0]
            bpp = struct.unpack_from("<H", orig, base + 14)[0]
            ncol = struct.unpack_from("<I", orig, base + 32)[0] or (1 << bpp if bpp <= 8 else 0)
            off = base + hs + 4 * ncol
            if off <= len(orig):
                return orig[:off] + bytes([_dominant(orig[off:])]) * (len(orig) - off)
    return None


def _dominant(b: bytes) -> int:
    """Most common byte of a pixel array: a flat fill of it compresses at least as well as the original."""
    if not b:
        return GREY
    from collections import Counter
    return Counter(b[:: max(1, len(b) // 65536)]).most_common(1)[0][0]


def neutral_jpeg(orig: bytes) -> bytes | None:
    if not orig.startswith(b"\xff\xd8"):
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(orig))
        w, h = im.size
        buf = io.BytesIO()
        Image.new("L", (w, h), GREY).save(buf, "JPEG", quality=50)
        base = buf.getvalue()
    except Exception:
        return None
    gap = len(orig) - len(base)
    if gap < 0:
        return None
    body, eoi = base[:-2], base[-2:]
    pads = []
    while gap > 0:
        if gap < 4:  # a COM segment is >= 4 bytes; put the remainder after EOI (ignored by decoders)
            return body + b"".join(pads) + eoi + b"\x00" * gap
        n = min(gap, 65537)
        if gap - n in (1, 2, 3):
            n -= 4
        pads.append(b"\xff\xfe" + struct.pack(">H", n - 2) + b"\x00" * (n - 4))
        gap -= n
    return body + b"".join(pads) + eoi


def neutral_image(orig: bytes) -> bytes | None:
    """Same-length neutral image, or None if the format is unknown / cannot be fitted."""
    for f in (neutral_png, neutral_jpeg, neutral_bmp):
        r = f(orig)
        if r is not None:
            assert len(r) == len(orig)
            return r
    return None


def neutral_any_size(orig: bytes) -> bytes | None:
    """For containers without length coupling (zip members): a small neutral image of the same size/format."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(orig))
        fmt = (im.format or "PNG").upper()
        buf = io.BytesIO()
        Image.new("L", im.size, GREY).save(buf, "JPEG" if fmt in ("JPEG", "JPG") else fmt if fmt in ("PNG", "BMP", "GIF") else "PNG")
        return buf.getvalue()
    except Exception:
        return None


def jpeg_extent(data: bytes, start: int) -> int | None:
    """End offset (exclusive) of the JPEG starting at `start` (FF D8), by walking its segments."""
    i, n = start + 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        m = data[i + 1]
        if m == 0xD9:
            return i + 2
        if m in (0x01,) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        ln = int.from_bytes(data[i + 2:i + 4], "big")
        if ln < 2:
            return None
        if m == 0xDA:  # start of scan: entropy-coded data until a marker that is not FF00 / RSTn
            j = i + 2 + ln
            while j + 1 < n:
                if data[j] == 0xFF and data[j + 1] not in (0x00,) and not 0xD0 <= data[j + 1] <= 0xD7:
                    break
                j += 1
            i = j
            continue
        i += 2 + ln
    return None


def neutralise_embedded_jpegs(data: bytes, min_size: int = 600) -> tuple[bytes, int]:
    """Replace every embedded JPEG (thumbnails in Creo / binary CAD) by a same-size neutral JPEG."""
    out, k, pos = bytearray(data), 0, 0
    while True:
        s = data.find(b"\xff\xd8\xff", pos)
        if s < 0:
            break
        e = jpeg_extent(data, s)
        if e is None or e - s < min_size:
            pos = s + 3
            continue
        n = neutral_jpeg(bytes(data[s:e]))
        if n is not None and len(n) == e - s:
            out[s:e] = n
            k += 1
        pos = e
    return bytes(out), k
