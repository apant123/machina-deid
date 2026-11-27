"""Stage 3 writers for text-based CAD / document formats. All edits are same-length, in place.

Every pseudonym keeps the width of its original in UTF-8 / UTF-16 / cp1252 / GBK, so a text
file is rewritten chunk by chunk (cut at newlines) without moving any byte: geometry, entity
numbering, IGES column layout and Hollerith counts stay valid by construction.

Escaped text is decoded, replaced and re-encoded with the *same escape kind per character*:
  STEP   \\X2\\hhhh..\\X0\\ (UTF-16), \\X4\\ (UTF-32), \\X\\hh (latin-1), \\S\\c
  DXF    \\U+hhhh (pre-2007 Unicode escapes)
A character that cannot be re-encoded the same way leaves the literal unchanged and is
reported (residual), never silently half-written.
"""
from __future__ import annotations

import mmap
import re
from pathlib import Path

from ..replace import Stats
from .mapping import DeidMap

CHUNK = 32 << 20


def stream_same_length(src: Path, dst: Path, fn, chunk: int = CHUNK) -> None:
    """dst = fn(chunk) for newline-cut chunks of src; fn must preserve length."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        size = src.stat().st_size
        if size == 0:
            return
        mm = mmap.mmap(fi.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            pos = 0
            while pos < size:
                end = min(size, pos + chunk)
                if end < size:
                    nl = mm.rfind(b"\n", pos, end)
                    end = nl + 1 if nl > pos else end
                buf = mm[pos:end]
                out = fn(buf)
                assert len(out) == len(buf), "writer changed length"
                fo.write(out)
                pos = end
        finally:
            mm.close()


# ---------------------------------------------------------------- escaped literals
_STEP_ESC = re.compile(rb"\\X2\\((?:[0-9A-Fa-f]{4})+)\\X0\\|\\X4\\((?:[0-9A-Fa-f]{8})+)\\X0\\|\\X\\([0-9A-Fa-f]{2})|\\S\\(.)|"
                       rb"\\P[A-I]\\|([^\\]+)|(\\)", re.S)


def _step_units(raw: bytes) -> list[tuple[str, str]] | None:
    units = []
    for m in _STEP_ESC.finditer(raw):
        if m.group(1):
            s = bytes.fromhex(m.group(1).decode()).decode("utf-16-be", "surrogatepass")
            units += [(c, "x2") for c in s]
        elif m.group(2):
            s = bytes.fromhex(m.group(2).decode()).decode("utf-32-be", "replace")
            units += [(c, "x4") for c in s]
        elif m.group(3):
            units.append((chr(int(m.group(3), 16)), "x"))
        elif m.group(4):
            units.append((chr(m.group(4)[0] + 128), "s"))
        elif m.group(5):
            units += [(c, "raw") for c in m.group(5).decode("latin-1")]
        elif m.group(6):
            units.append(("\\", "raw"))
        else:  # \PA\ code-page directive: keep as one opaque unit
            units.append(("\x00" + m.group().decode("latin-1"), "dir"))
    return units


def _step_encode(units, new: str) -> bytes | None:
    if len(new) != len(units):
        return None
    out, x2, x4 = [], [], []

    def flush():
        if x2:
            out.append(b"\\X2\\" + "".join(x2).encode("utf-16-be", "surrogatepass").hex().upper().encode() + b"\\X0\\")
            x2.clear()
        if x4:
            out.append(b"\\X4\\" + "".join(x4).encode("utf-32-be").hex().upper().encode() + b"\\X0\\")
            x4.clear()
    prev = None
    for (c0, kind), c in zip(units, new):
        if kind != prev or kind not in ("x2", "x4"):
            flush()
        prev = kind
        if kind == "dir":
            out.append(c0[1:].encode("latin-1"))
        elif kind == "x2":
            x2.append(c)
        elif kind == "x4":
            x4.append(c)
        elif kind == "x":
            if ord(c) > 255:
                return None
            out.append(b"\\X\\%02X" % ord(c))
        elif kind == "s":
            if not 160 <= ord(c) <= 255:
                return None
            out.append(b"\\S\\" + bytes([ord(c) - 128]))
        else:  # raw byte(s)
            try:
                out.append(c.encode("latin-1"))
            except UnicodeEncodeError:
                return None
    flush()
    return b"".join(out)


_LIT_WITH_ESC = re.compile(rb"'((?:[^'\r\n]|'')*\\(?:X2|X4|X|S)\\(?:[^'\r\n]|'')*)'")


def step_fn(dm: DeidMap, st: Stats):
    def fn(buf: bytes) -> bytes:
        out = bytearray(dm.apply(buf, st))
        for m in _LIT_WITH_ESC.finditer(out):  # names hidden in \X2\ etc. escapes
            raw = bytes(m.group(1))
            units = _step_units(raw)
            text = "".join(c for c, k in units if k != "dir")
            if not text:
                continue
            new = dm.value(text, st)
            if new == text:
                continue
            it = iter(new)
            merged = "".join(c if k == "dir" else next(it) for c, k in units)
            enc = _step_encode(units, merged)
            if enc is None or len(enc) != len(raw):
                st.samples.append(b"step-escape-unencodable:" + raw[:60])
                continue
            out[m.start(1):m.end(1)] = enc
        return bytes(out)
    return fn


# ---------------------------------------------------------------- IGES
def iges_rewrite(src: Path, dst: Path, dm: DeidMap, st: Stats) -> None:
    """S/G/P sections: data columns are joined into one virtual text per section (strings wrap across
    80-column lines), replaced (same length), and written back to the same byte positions."""
    data = bytearray(src.read_bytes())
    sections: dict[str, list[tuple[int, int]]] = {"S": [], "G": [], "P": []}
    for m in re.finditer(rb"[^\r\n]+", data):
        line_s, line = m.start(), m.group()
        if len(line) >= 73 and chr(line[72]) in sections:
            sections[chr(line[72])].append((line_s, 72 if chr(line[72]) != "P" else 64))
    for sec, spans in sections.items():
        if not spans:
            continue
        virt = b"".join(bytes(data[s:s + n]) for s, n in spans)
        new = bytearray(dm.apply(virt, st))
        # Hollerith strings are complete field values ('6Hquorv,' -> author field)
        for hm in re.finditer(rb"(?:^|[,;])\s*(\d+)H", new):
            n = int(hm.group(1))
            a = hm.end()
            if n <= 0 or a + n > len(new):
                continue
            raw = bytes(new[a:a + n])
            try:
                s = raw.decode("latin-1")
            except UnicodeDecodeError:
                continue
            v = dm.value(s, st)
            if v != s:
                vb = v.encode("latin-1", "replace")
                if len(vb) == n:
                    new[a:a + n] = vb
        if bytes(new) == virt:
            continue
        pos = 0
        for s, n in spans:
            data[s:s + n] = new[pos:pos + n]
            pos += n
    dst.write_bytes(bytes(data))


# ---------------------------------------------------------------- DXF \U+ escapes
_DXF_U = re.compile(rb"(?:\\U\+[0-9A-Fa-f]{4})+")


def dxf_fn(dm: DeidMap, st: Stats):
    def fn(buf: bytes) -> bytes:
        out = bytearray(dm.apply(buf, st))
        for m in _DXF_U.finditer(out):
            raw = bytes(m.group())
            s = "".join(chr(int(raw[i + 3:i + 7], 16)) for i in range(0, len(raw), 7))
            # the escaped run is usually part of a longer value: decode the whole line value around it
            ls = out.rfind(b"\n", 0, m.start()) + 1
            le = out.find(b"\n", m.end())
            le = len(out) if le < 0 else le
            line = bytes(out[ls:le])
            dec = _DXF_U.sub(lambda mm: "".join(chr(int(mm.group()[i + 3:i + 7], 16))
                                                for i in range(0, len(mm.group()), 7)).encode("utf-8"), line)
            text = dec.decode("utf-8", "replace")
            new = dm.text(text, st)
            if new == text:
                continue
            # re-encode: ASCII stays raw, other chars as \U+hhhh  (same widths -> same layout)
            enc = "".join(c if ord(c) < 128 else f"\\U+{ord(c):04X}" for c in new).encode()
            if len(enc) == len(line):
                out[ls:le] = enc
            else:
                st.samples.append(b"dxf-escape-unencodable:" + s.encode("utf-8")[:40])
        return bytes(out)
    return fn


# ---------------------------------------------------------------- dispatch
def rewrite_text(fmt: str, src: Path, dst: Path, dm: DeidMap, st: Stats) -> None:
    if fmt == "iges":
        return iges_rewrite(src, dst, dm, st)
    fn = step_fn(dm, st) if fmt == "step" else dxf_fn(dm, st) if fmt == "dxf" else (lambda b: dm.apply(b, st))
    stream_same_length(src, dst, fn)


def rewrite_stl_binary(src: Path, dst: Path, dm: DeidMap, st: Stats) -> None:
    """Only the 80-byte header is text; the triangles are copied untouched."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        head = fi.read(80)
        fo.write(dm.apply(head, st))
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(b)
