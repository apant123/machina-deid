"""Spike: size-preserving rewrite of SolidWorks 2015+ container files.

Strategy: for each real section (marker 14 00 06 00 08 00, CRC32-verified):
  decompress -> same-length byte replacements -> recompress to EXACTLY the
  original compressed length -> patch CRC32 everywhere the old CRC is recorded
  for that section (local header + central-directory entry).
File size, every section offset, every compressed/uncompressed size: unchanged.
"""
from __future__ import annotations

import re
import struct
import sys
import zlib
from pathlib import Path

MARK = b"\x14\x00\x06\x00\x08\x00"


def nib(b: bytes) -> bytes:
    return bytes(((x << 4) & 0xF0) | (x >> 4) for x in b)


def parse(d: bytes):
    """Return real sections: dicts with hdr (offset of field x), ds, b, c, crc, name, data."""
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
                        out.append(dict(hdr=h, ds=ds, b=b, c=c, crc=a, x=x,
                                        name=nib(d[h + 20:ds]).decode("latin1"), data=dec))
                        pos = d.find(MARK, ds + b)
                        continue
                except zlib.error:
                    pass
        pos = d.find(MARK, pos + 1)
    return out


def _deflate(data: bytes, level: int, mem: int, strategy: int, syncs: int = 0, partials: int = 0) -> bytes:
    co = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strategy)
    out = co.compress(data)
    for _ in range(partials):
        out += co.flush(zlib.Z_PARTIAL_FLUSH)
    for _ in range(syncs):
        out += co.flush(zlib.Z_SYNC_FLUSH)
    return out + co.flush(zlib.Z_FINISH)


EMPTY_STORED = b"\x00\x00\x00\xff\xff"  # non-final empty stored block: exactly 5 bytes, byte-aligned


def _zopfli(data: bytes, iters: int = 15) -> bytes:
    import zopfli.zopfli as zz
    return zz.compress(data, numiterations=iters)[2:-4]  # strip zlib header + adler32 -> raw deflate


def _stored_prefix(data: bytes, m: int, tail: bytes) -> bytes:
    """Non-final stored block carrying data[:m], followed by a raw stream for data[m:]."""
    return b"\x00" + struct.pack("<HH", m, m ^ 0xFFFF) + data[:m] + tail


def deflate_exact(data: bytes, target: int) -> bytes | None:
    """Raw-deflate `data` into exactly `target` bytes, or None.

    1. Build a few candidate streams (zlib configs, zopfli). Any candidate shorter than
       target by a multiple of 5 is padded with empty stored blocks (5 bytes each).
    2. Otherwise move m leading bytes into a stored block (+5+m bytes, minus whatever the
       compressor saved) and repeat; this shifts the length residue mod 5.
    """
    def fit(stream: bytes) -> bytes | None:
        gap = target - len(stream)
        if gap >= 0 and gap % 5 == 0:
            return EMPTY_STORED * (gap // 5) + stream
        return None

    # 1) Plain configs (the original encoder's config reproduces unchanged data exactly).
    for level, mem, strat in ((6, 8, 0), (9, 9, 0), (9, 8, 0), (5, 8, 0), (7, 8, 0), (8, 8, 0)):
        if (r := fit(_deflate(data, level, mem, strat))) is not None:
            return r
    # 2) Split-flush variants: compress most of the data once, then branch the
    #    compressor state and insert a partial/sync flush at varying split points.
    #    Each branch ends at a different bit alignment -> different length residue.
    for level, mem in ((9, 9), (6, 8)):
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
    # 3) Tight budget (edited data compresses worse): zopfli is ~3-8% smaller than zlib -9.
    big = len(data) > 2_000_000
    for iters, split_max in ((15, 15), (5, 15), (15, 0), (30, 15), (10, 5), (20, 10), (8, 3), (12, 8)):
        if big and iters > 5:
            continue
        import zopfli.zopfli as zz
        z = zz.compress(data, numiterations=iters, blocksplittingmax=split_max)[2:-4]
        if (r := fit(z)) is not None:
            return r
    return None


def rewrite(d: bytes, replacer) -> tuple[bytes, dict]:
    """`replacer`: deid.replace.Replacer (structure-safe, boundary-aware, same-length)."""
    out = bytearray(d)
    stats = dict(sections=0, changed=0, exact_fail=[], crc_copies=[], replaced=0, skipped_inside_token=0,
                 skipped_structural=0, residual=set())
    for s in parse(d):
        stats["sections"] += 1
        new, st = replacer.apply(s["data"])
        stats["replaced"] += st.replaced
        stats["skipped_inside_token"] += st.skipped_inside_token
        stats["skipped_structural"] += st.skipped_structural
        stats["residual"] |= set(replacer.residual(new))
        if new == s["data"]:
            continue
        comp = deflate_exact(new, s["b"])
        if comp is None:
            stats["exact_fail"].append(s["name"])
            continue
        out[s["ds"]:s["ds"] + s["b"]] = comp
        old_crc, new_crc = struct.pack("<I", s["crc"]), struct.pack("<I", zlib.crc32(new))
        # Patch every directory/local entry recording this section: marker + x + crc + b + c
        sig = MARK + struct.pack("<IIII", s["x"], s["crc"], s["b"], s["c"])
        n, p = 0, out.find(sig)
        while p != -1:
            out[p + 10:p + 14] = new_crc
            n += 1
            p = out.find(sig, p + 1)
        stats["crc_copies"].append(n)
        stats["changed"] += 1
    return bytes(out), stats


def roundtrip_all(d: bytes) -> dict:
    """No-op test: can every section be recompressed to its exact original size?"""
    secs = parse(d)
    fails = [s["name"] for s in secs if deflate_exact(s["data"], s["b"]) is None]
    return dict(sections=len(secs), exact_fit_fail=len(fails), fail_names=fails[:5])


_CLASS = re.compile(rb"[a-z]{2}[A-Z][A-Za-z0-9]*_c(?![A-Za-z0-9_])")
_CLASS16 = re.compile(rb"(?:[a-z]\x00){2}[A-Z]\x00(?:[A-Za-z0-9]\x00)*_\x00c\x00")
_XMLNAME = re.compile(rb"</?([A-Za-z_][\w.:-]*)|\s([A-Za-z_][\w.:-]*)=[\"']")


def structural_tokens(data: bytes) -> list:
    """MFC class names + XML tag/attribute names: must survive a rewrite byte-identically."""
    return sorted(_CLASS.findall(data) + _CLASS16.findall(data) +
                  [a or b for a, b in _XMLNAME.findall(data)])


def verify(orig: bytes, new: bytes) -> dict:
    a, b = parse(orig), parse(new)
    return dict(same_size=len(orig) == len(new), same_section_count=len(a) == len(b),
                same_layout=[(s["hdr"], s["b"], s["c"]) for s in a] == [(s["hdr"], s["b"], s["c"]) for s in b],
                all_crc_ok=all(zlib.crc32(s["data"]) == s["crc"] for s in b),
                structure_preserved=all(structural_tokens(x["data"]) == structural_tokens(y["data"]) for x, y in zip(a, b)))


if __name__ == "__main__" and sys.argv[1:] == ["--selftest"]:
    _selftest()
elif __name__ == "__main__":
    import re
    for f in sys.argv[1:]:
        d = Path(f).read_bytes()
        rt = roundtrip_all(d)
        # Build a toy same-length mapping from usernames seen in C:\Users\<name>\ paths.
        users = set()
        for s in parse(d):
            users |= set(re.findall(rb"\\Users\\([A-Za-z][A-Za-z0-9._-]{2,30})\\", s["data"]))
            users |= set(re.findall(rb"lastModifiedBy>([^<]{3,30})<", s["data"]))
        mapping = {u: (b"U" + bytes(ord("a") + (i * 7 + j) % 26 for j in range(len(u) - 1))) for i, u in enumerate(sorted(users))}
        new, st = rewrite(d, mapping)
        v = verify(d, new)
        leaks = sum(new.count(u) for u in users)  # raw bytes outside sections (should be 0 if all in sections)
        leaks_dec = sum(s["data"].count(u) for s in parse(new) for u in users)
        print(f"{Path(f).name}: roundtrip={rt} | users={sorted(x.decode() for x in users)} | "
              f"changed={st['changed']} exact_fail={st['exact_fail']} crc_copies_patched={st['crc_copies']} | verify={v} | "
              f"remaining_in_sections={leaks_dec} raw={leaks}")


def _selftest():
    import os
    for n in (50, 5000, 200000):
        data = (b"swPath=C:\\Users\\alice\\proj\\" + os.urandom(8)) * (n // 30)
        base = len(_deflate(data, 6, 8, 0))
        for target in (base, base + 1, base + 2, base + 3, base + 4, base + 17):
            out = deflate_exact(data, target)
            assert out is not None and len(out) == target, (n, target)
            do = zlib.decompressobj(-15)
            assert do.decompress(out) == data and do.eof and not do.unused_data
    print("deflate_exact selftest ok")
