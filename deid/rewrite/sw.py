"""SolidWorks 2015+ container writer (size-preserving; see docs/SPIKES.md).

Per CRC-verified section: inflate -> map (same length) -> previews neutralised -> deflate to EXACTLY
the original compressed length -> patch the CRC in every header that records the section (local
header + central directory). Section names (nibble-swapped) are renamed in place, same length.
File size, offsets and every size field stay identical. If any changed section cannot be fitted
to its exact size, the whole file is quarantined (never shipped half-rewritten).
"""
from __future__ import annotations

import re
import struct
import zlib

from ..replace import Stats
from .mapping import DeidMap
from .thumbs import neutral_image, neutral_png

MARK = b"\x14\x00\x06\x00\x08\x00"
PREVIEW = re.compile(r"(?i)(^|/)(preview(png)?|thumbnail[^/]*)$")


def nib(b: bytes) -> bytes:
    return bytes(((x << 4) & 0xF0) | (x >> 4) for x in b)


def parse(d: bytes) -> list[dict]:
    out, pos = [], d.find(MARK)
    while pos != -1:
        h = pos + 6
        if h + 20 <= len(d):
            x, a, b, c, nlen = struct.unpack_from("<IIIII", d, h)
            ds = h + 20 + nlen
            if 0 < b <= len(d) - ds and nlen < 400:
                try:
                    do = zlib.decompressobj(-15)
                    dec = do.decompress(d[ds:ds + b])
                    if do.eof and len(dec) == c and zlib.crc32(dec) == a:
                        out.append(dict(hdr=h, ds=ds, b=b, c=c, crc=a, x=x, nlen=nlen,
                                        name=nib(d[h + 20:ds]).decode("latin1"), data=dec))
                        pos = d.find(MARK, ds + b)
                        continue
                except zlib.error:
                    pass
        pos = d.find(MARK, pos + 1)
    return out


EMPTY_STORED = b"\x00\x00\x00\xff\xff"


def _deflate(data: bytes, level: int, mem: int, strategy: int) -> bytes:
    co = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strategy)
    return co.compress(data) + co.flush(zlib.Z_FINISH)


def deflate_exact(data: bytes, target: int) -> bytes | None:
    """Raw-deflate `data` into exactly `target` bytes (padding with 5-byte empty stored blocks), or None."""
    def fit(stream: bytes) -> bytes | None:
        gap = target - len(stream)
        if gap >= 0 and gap % 5 == 0:
            return EMPTY_STORED * (gap // 5) + stream
        return None

    for level, mem, strat in ((6, 8, 0), (9, 9, 0), (9, 8, 0), (5, 8, 0), (7, 8, 0), (8, 8, 0), (1, 8, 0), (9, 9, 1)):
        if (r := fit(_deflate(data, level, mem, strat))) is not None:
            return r
    for level, mem in ((9, 9), (6, 8)):  # branch the compressor and flush at varying points -> other residues
        tail = min(len(data), 256)
        co = zlib.compressobj(level, zlib.DEFLATED, -15, mem)
        head = co.compress(data[: len(data) - tail])
        rest = data[len(data) - tail:]
        for j in range(0, tail + 1, max(1, tail // 48)):
            for flush in (zlib.Z_PARTIAL_FLUSH, zlib.Z_SYNC_FLUSH, zlib.Z_BLOCK):
                c2 = co.copy()
                s = head + c2.compress(rest[:j]) + c2.flush(flush) + c2.compress(rest[j:]) + c2.flush(zlib.Z_FINISH)
                if (r := fit(s)) is not None:
                    return r
    if len(data) <= 4_000_000:  # wider encoder grid: fixed Huffman / RLE / memLevels land on other lengths
        for strat in (0, 1, 3, 4):
            for level in range(1, 10):
                for mem in (1, 3, 5, 7, 9):
                    if (r := fit(_deflate(data, level, mem, strat))) is not None:
                        return r
    # stored prefix: data[:m] in a non-final stored block (5 + m bytes) + compressed rest -> shifts the residue
    for level, mem in ((9, 9), (6, 8)):
        for m in range(1, min(len(data), 4096)):
            rest = _deflate(data[m:], level, mem, 0)
            if 5 + m + len(rest) > target:
                break
            if (r := fit(b"\x00" + struct.pack("<HH", m, m ^ 0xFFFF) + data[:m] + rest)) is not None:
                return r
    import zopfli.zopfli as zz
    big = len(data) > 2_000_000
    for iters, split_max in ((15, 15), (5, 15), (15, 0), (30, 15), (10, 5), (20, 10), (8, 3), (12, 8)):
        if big and iters > 5:
            continue
        if (r := fit(zz.compress(data, numiterations=iters, blocksplittingmax=split_max)[2:-4])) is not None:
            return r
    return None


_CLASS = re.compile(rb"[a-z]{2}[A-Z][A-Za-z0-9]*_c(?![A-Za-z0-9_])")
_CLASS16 = re.compile(rb"(?:[a-z]\x00){2}[A-Z]\x00(?:[A-Za-z0-9]\x00)*_\x00c\x00")
_XMLNAME = re.compile(rb"</?([A-Za-z_][\w.:-]+)|\s([A-Za-z_][\w.:-]+)=[\"']")  # >= 2 chars: '<B' in UTF-16 is not a tag


def structural_tokens(data: bytes) -> list:
    return sorted(_CLASS.findall(data) + _CLASS16.findall(data) + [a or b for a, b in _XMLNAME.findall(data)])


def fit_preview(orig: bytes, target: int) -> tuple[bytes | None, bytes]:
    """A neutral PNG compresses far below the original size: add incompressible padding until the raw-deflate
    length lands on target minus a multiple of 5 (then padded with empty stored blocks)."""
    base = neutral_png(orig, 0)
    if base is None:
        return None, orig
    lo = len(_deflate(base, 9, 9, 0))
    k0 = max(0, target - lo - 64)
    for k in list(range(k0, k0 + 80)) + list(range(max(0, k0 - 400), k0)):
        img = neutral_png(orig, k)
        if img is None:
            break
        z = _deflate(img, 9, 9, 0)
        gap = target - len(z)
        if gap >= 0 and gap % 5 == 0:
            return EMPTY_STORED * (gap // 5) + z, img
    return None, orig


def rewrite(d: bytes, dm: DeidMap, st: Stats) -> tuple[bytes | None, dict]:
    """-> (new bytes or None if quarantined, info)."""
    out = bytearray(d)
    info = dict(sections=0, changed=0, previews=0, preview_kept=0, renamed=0, exact_fail=[], structure_broken=[])
    secs = parse(d)
    info["sections"] = len(secs)
    for s in secs:
        data = s["data"]
        if PREVIEW.search(s["name"]):
            new = neutral_image(data)
            if new is None:
                new = dm.apply(data, st)
            else:
                info["previews"] += 1
        else:
            new = dm.apply(data, st)
            if structural_tokens(new) != structural_tokens(data):
                info["structure_broken"].append(s["name"])
                return None, info
        # section name (e.g. a configuration named after a person), same length, both copies
        nm = dm.text(s["name"], st)
        if nm != s["name"] and len(nm.encode("latin1", "replace")) == s["nlen"]:
            old_n, new_n = nib(s["name"].encode("latin1")), nib(nm.encode("latin1", "replace"))
            sig = struct.pack("<I", s["nlen"]) + old_n
            p = out.find(sig)
            while p != -1:
                out[p + 4:p + 4 + s["nlen"]] = new_n
                p = out.find(sig, p + 1)
            info["renamed"] += 1
        if new == data:
            continue
        comp = deflate_exact(new, s["b"])
        if comp is None and PREVIEW.search(s["name"]):
            comp, new = fit_preview(data, s["b"])
            if comp is None:  # cannot fit a neutral image: keep the original pixels (text-replaced only)
                new = dm.apply(data, st)
                comp = s["b"] and (deflate_exact(new, s["b"]) if new != data else None)
                info["previews"] -= 1
                info["preview_kept"] = info.get("preview_kept", 0) + 1
                if new == data:
                    continue
        if comp is None:
            info["exact_fail"].append(s["name"])
            return None, info
        out[s["ds"]:s["ds"] + s["b"]] = comp
        sig = MARK + struct.pack("<IIII", s["x"], s["crc"], s["b"], s["c"])
        new_crc = struct.pack("<I", zlib.crc32(new))
        p = out.find(sig)
        while p != -1:
            out[p + 10:p + 14] = new_crc
            p = out.find(sig, p + 1)
        info["changed"] += 1
    return bytes(out), info


def verify(orig: bytes, new: bytes) -> dict:
    a, b = parse(orig), parse(new)
    ok = dict(same_size=len(orig) == len(new), same_sections=len(a) == len(b),
              same_layout=[(s["hdr"], s["b"], s["c"]) for s in a] == [(s["hdr"], s["b"], s["c"]) for s in b],
              crc_ok=all(zlib.crc32(s["data"]) == s["crc"] for s in b),
              structure_preserved=all(structural_tokens(x["data"]) == structural_tokens(y["data"])
                                      for x, y in zip(a, b) if not PREVIEW.search(x["name"])))
    ok["ok"] = all(ok.values())
    return ok
