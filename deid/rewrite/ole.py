"""OLE2 / Compound File writer (SolidWorks < 2015, Inventor, Solid Edge, Altium, CATIA V4 OLE, ...).

All edits are same-length, so every stream keeps its sector chain and the file layout is
untouched: each stream is read from its FAT / mini-FAT chain, transformed, and written back to the
same byte positions.

Per stream:
  * the pseudonym map (global / whole-value / path-component passes);
  * `*__ZLB` streams (old SolidWorks): [header][zlib] -> inflate, map, deflate to the exact size;
  * preview / thumbnail streams -> neutral image of the same size;
  * SummaryInformation: PIDSI_THUMBNAIL (VT_CF) DIB pixels -> neutral fill (text values are
    VT_LPSTR / VT_LPWSTR, i.e. length-prefixed, and handled by the whole-value pass).
Leak hygiene (the spike found names in stale data): FREE big sectors, free mini sectors, stream tail
slack and unused directory entries are zeroed.
Verify: re-open, same stream list and sizes, every stream readable, MFC class / XML names intact.
"""
from __future__ import annotations

import re
import struct
import zlib

from ..replace import Stats
from .mapping import DeidMap
from .sw import deflate_exact, structural_tokens
from .thumbs import neutral_bmp, neutral_image

FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
PREVIEW = re.compile(r"(?i)(preview|thumbnail)")


class OleError(Exception):
    pass


def _chain(fat, start: int, limit: int) -> list[int]:
    out, seen = [], set()
    s = start
    while s not in (ENDOFCHAIN, FREESECT) and s < len(fat):
        if s in seen or len(out) > limit:
            raise OleError("cyclic / overlong chain")
        seen.add(s)
        out.append(s)
        s = fat[s]
    return out


class _Layout:
    def __init__(self, buf: bytearray):
        import olefile
        import io
        self.ole = olefile.OleFileIO(io.BytesIO(bytes(buf)))
        o = self.ole
        self.ss, self.mss = o.sectorsize, o.minisectorsize
        self.cutoff = o.minisectorcutoff
        self.fat = list(o.fat)
        o.loadminifat()
        self.minifat = list(o.minifat) if o.minifat is not None else []
        root = o.root
        self.root_chain = _chain(self.fat, root.isectStart, len(self.fat) + 1) if root.size else []
        self.root_size = root.size
        self.buf = buf

    def off(self, sect: int) -> int:
        return (sect + 1) * self.ss

    def stream_spans(self, start: int, size: int) -> list[tuple[int, int]]:
        """File (offset, length) pieces holding the stream's bytes, in order."""
        spans = []
        if size < self.cutoff:
            for m in _chain(self.minifat, start, len(self.minifat) + 1):
                pos = m * self.mss
                big = self.root_chain[pos // self.ss]
                spans.append((self.off(big) + pos % self.ss, self.mss))
        else:
            for s in _chain(self.fat, start, len(self.fat) + 1):
                spans.append((self.off(s), self.ss))
        out, left = [], size
        for o, n in spans:
            k = min(n, left)
            out.append((o, k, n))
            left -= k
        return [(o, k, n) for o, k, n in out]

    def read(self, spans) -> bytes:
        return b"".join(bytes(self.buf[o:o + k]) for o, k, _ in spans)

    def write(self, spans, data: bytes) -> None:
        pos = 0
        size = len(self.buf)
        for o, k, n in spans:
            self.buf[o:o + k] = data[pos:pos + k]
            end = min(o + n, size)  # truncated files: the last sector may be partial
            if end > o + k:  # tail slack of the last sector
                self.buf[o + k:end] = b"\x00" * (end - o - k)
            pos += k


def _zlb(data: bytes, dm: DeidMap, st: Stats) -> bytes | None:
    for off in range(0, min(64, len(data) - 2)):
        if data[off] == 0x78 and data[off + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            do = zlib.decompressobj()
            try:
                inner = do.decompress(data[off:], 400_000_000)
            except zlib.error:
                continue
            if not do.eof:
                continue
            used = len(data) - off - len(do.unused_data)
            new = dm.apply(inner, st)
            if new == inner:
                return data
            if structural_tokens(new) != structural_tokens(inner):
                raise OleError("structure tokens changed inside __ZLB")
            raw = deflate_exact(new, used - 6)
            if raw is None:
                raise OleError("__ZLB exact size")
            blob = data[off:off + 2] + raw + struct.pack(">I", zlib.adler32(new))
            return data[:off] + blob + data[off + used:]
    return data


_VT_CF = 0x47


def _thumbnail_props(data: bytes) -> bytes:
    """SummaryInformation: neutralise PIDSI_THUMBNAIL (17) DIB pixels in place."""
    try:
        if len(data) < 48 or data[:2] != b"\xfe\xff":
            return data
        nsets = struct.unpack_from("<I", data, 24)[0]
        out = bytearray(data)
        for k in range(min(nsets, 4)):
            so = struct.unpack_from("<I", data, 28 + 20 * k + 16)[0]
            _, nprop = struct.unpack_from("<II", data, so)
            for j in range(min(nprop, 256)):
                pid, po = struct.unpack_from("<II", data, so + 8 + 8 * j)
                p = so + po
                if pid != 17 or struct.unpack_from("<I", data, p)[0] != _VT_CF:
                    continue
                size, cf = struct.unpack_from("<Ii", data, p + 4)
                if cf == -1:
                    fmt = struct.unpack_from("<I", data, p + 12)[0]
                    body_o, body_n = p + 16, size - 8
                    if fmt == 8 and 0 < body_n <= len(data) - body_o:  # CF_DIB
                        nb = neutral_bmp(bytes(data[body_o:body_o + body_n]))
                        if nb is not None:
                            out[body_o:body_o + body_n] = nb
        return bytes(out)
    except (struct.error, IndexError):
        return data


def rewrite(d: bytes, dm: DeidMap, st: Stats) -> tuple[bytes | None, dict]:
    buf = bytearray(d)
    info = dict(streams=0, changed=0, previews=0, zlb=0, wiped_sectors=0, wiped_mini=0, wiped_dir=0, error=None)
    try:
        L = _Layout(buf)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"parse: {e!r}"[:200]
        return None, info
    ole = L.ole
    entries = [e for e in ole.direntries if e is not None]
    used_mini = set()
    for e in entries:
        if e.entry_type != 2:  # STGTY_STREAM
            continue
        info["streams"] += 1
        try:
            spans = L.stream_spans(e.isectStart, e.size)
        except OleError as ex:
            info["error"] = f"chain {e.name}: {ex}"
            return None, info
        if e.size < L.cutoff:
            used_mini.update(_chain(L.minifat, e.isectStart, len(L.minifat) + 1))
        data = L.read(spans)
        name = e.name
        new = data
        try:
            if PREVIEW.search(name) and (n := neutral_image(data)) is not None:
                new = n
                info["previews"] += 1
            else:
                if name.lower().endswith("__zlb"):
                    z = _zlb(data, dm, st)
                    if z is not data:
                        info["zlb"] += 1
                    new = z
                new = dm.apply(new, st)
                if "SummaryInformation" in name:
                    new = _thumbnail_props(new)
                if structural_tokens(new) != structural_tokens(data) and not name.lower().endswith("__zlb"):
                    info["error"] = f"structure tokens changed in {name}"
                    return None, info
        except OleError as ex:
            info["error"] = f"{name}: {ex}"
            return None, info
        if len(new) != len(data):
            info["error"] = f"length changed in {name}"
            return None, info
        L.write(spans, new)  # also zeroes the tail slack of the last sector
        info["changed"] += new != data
    # ---- leak hygiene -----------------------------------------------------------------------
    nsect = (len(buf) // L.ss) - 1
    for i, v in enumerate(L.fat[:nsect]):
        if v == FREESECT and L.off(i) + L.ss <= len(buf) and any(buf[L.off(i):L.off(i) + L.ss]):
            buf[L.off(i):L.off(i) + L.ss] = b"\x00" * L.ss
            info["wiped_sectors"] += 1
    if L.root_chain:  # every mini sector not owned by a live stream (free, or stale beyond the ministream)
        for m in range(len(L.root_chain) * L.ss // L.mss):
            if m in used_mini:
                continue
            pos = m * L.mss
            o = L.off(L.root_chain[pos // L.ss]) + pos % L.ss
            n = max(0, min(L.mss, len(buf) - o))
            if n and any(buf[o:o + n]):
                buf[o:o + n] = b"\x00" * n
                info["wiped_mini"] += 1
    # unused directory entries (type 0) may keep names of deleted streams
    dir_chain = _chain(L.fat, ole.first_dir_sector if hasattr(ole, "first_dir_sector") else
                       struct.unpack_from("<I", d, 48)[0], len(L.fat) + 1)
    for s in dir_chain:
        base = L.off(s)
        for k in range(L.ss // 128):
            o = base + 128 * k
            if o + 128 <= len(buf) and buf[o + 66] == 0:  # entry type EMPTY
                if any(buf[o:o + 128]):
                    buf[o:o + 128] = b"\x00" * 64 + b"\x00\x00" + b"\x00\x00" + b"\xff" * 12 + b"\x00" * 48
                    info["wiped_dir"] += 1
    out = bytes(buf)
    v = verify(d, out)
    if not v["ok"]:
        info["error"] = f"verify: {v}"
        return None, info
    return out, info


def verify(orig: bytes, new: bytes) -> dict:
    import io
    import olefile
    try:
        a, b = olefile.OleFileIO(io.BytesIO(orig)), olefile.OleFileIO(io.BytesIO(new))
        la, lb = a.listdir(streams=True, storages=True), b.listdir(streams=True, storages=True)
        same = la == lb and all(a.get_size("/".join(s)) == b.get_size("/".join(s)) for s in a.listdir(streams=True, storages=False))
        readable = all(len(b.openstream(s).read()) == b.get_size("/".join(s)) for s in b.listdir(streams=True, storages=False))
        r = dict(same_size=len(orig) == len(new), same_streams=same, readable=readable)
    except Exception as e:  # noqa: BLE001
        r = dict(error=repr(e)[:200], same_size=False)
    r["ok"] = all(v for k, v in r.items() if k != "error") and "error" not in r
    return r
