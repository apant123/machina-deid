"""Same-length writers for binary / mixed formats: Creo #UGC, generic binary CAD (Parasolid binary, JT,
U3D, CATIA, I-DEAS, 3DS, native prt/asm, unknown), and the 80-byte binary-STL header.

Creo: the hex field on line 1 is the byte offset of the end of the `#END_OF_P_OBJECT` line, so
same-length edits keep it valid; it is re-checked after the rewrite. Embedded JPEG thumbnails
(Creo 2+) are replaced by same-size neutral JPEGs.
JT: names also live inside zlib-compressed segments -> inflated, replaced, deflated to the exact size.
"""
from __future__ import annotations

import re
import struct
import zlib

from ..replace import Stats
from .mapping import DeidMap
from .sw import deflate_exact, structural_tokens
from .thumbs import neutralise_embedded_jpegs

_UGC = re.compile(rb"^#UGC:\d+ \w+ [\d -]+? ([0-9a-f]{8}) ")


def creo_offset_ok(d: bytes) -> bool | None:
    m = _UGC.match(d[:200])
    if not m:
        return None  # older files have no offset field
    off = int(m.group(1), 16)
    i = d.find(b"#END_OF_P_OBJECT")
    if i < 0:
        return None
    nl = d.find(b"\n", i)
    return off in (nl, nl + 1, i + len(b"#END_OF_P_OBJECT"))


def rewrite_creo(d: bytes, dm: DeidMap, st: Stats) -> tuple[bytes | None, dict]:
    before = creo_offset_ok(d)
    new = dm.apply(d, st)
    new, k = neutralise_embedded_jpegs(new)
    info = dict(thumbnails=k, offset_ok=creo_offset_ok(new))
    if len(new) != len(d) or before is True and info["offset_ok"] is not True:
        info["error"] = "creo layout changed"
        return None, info
    return new, info


def _zlib_segments(d: bytes):
    for m in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", d):
        s = m.start()
        do = zlib.decompressobj()
        try:
            inner = do.decompress(d[s:s + 64_000_000], 400_000_000)
        except zlib.error:
            continue
        if do.eof and len(inner) >= 32:
            yield s, len(d) - s - len(do.unused_data) if len(d) - s <= 64_000_000 else None, inner


def rewrite_binary(d: bytes, dm: DeidMap, st: Stats, inflate: bool = False) -> tuple[bytes | None, dict]:
    """Generic: map over raw bytes; optionally also inside zlib segments (JT), exact-size recompression."""
    out = bytearray(dm.apply(d, st))
    info = dict(zlib_changed=0, zlib_fail=0)
    if inflate:
        pos = 0
        for s, used, inner in _zlib_segments(bytes(out)):
            if used is None or s < pos:
                continue
            new = dm.apply(inner, st)
            if new == inner:
                pos = s + used
                continue
            raw = deflate_exact(new, used - 6)
            if raw is None or structural_tokens(new) != structural_tokens(inner):
                info["zlib_fail"] += 1
                continue
            out[s:s + used] = bytes(out[s:s + 2]) + raw + struct.pack(">I", zlib.adler32(new))
            info["zlib_changed"] += 1
            pos = s + used
        if info["zlib_fail"]:
            info["error"] = "zlib segment exact size"
            return None, info
    out2, k = neutralise_embedded_jpegs(bytes(out))
    info["thumbnails"] = k
    return out2, info
