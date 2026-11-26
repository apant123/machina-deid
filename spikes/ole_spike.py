"""Spike: OLE2 CAD (old SolidWorks, Inventor, Solid Edge) same-length rewrite.

For each file:
  - list streams; find identifying strings (ASCII + UTF-16LE) per stream, raw and
    inside any zlib/raw-deflate payloads
  - rewrite streams in place with same-length replacements via olefile.write_stream
  - re-open, compare stream list + sizes, confirm names gone, check property sets parse
"""
from __future__ import annotations

import re
import shutil
import sys
import zlib
from pathlib import Path

import olefile

PATS = [rb"\\Users\\([^\\\x00]{3,30})\\", rb"Documents and Settings\\([^\\\x00]{3,30})\\",
        rb"[\w.+-]{2,40}@[\w-]{2,40}\.[\w.]{2,10}"]


def utf16(b: bytes) -> bytes:
    return b.decode("latin1").encode("utf-16le")


def find_names(blob: bytes) -> set[bytes]:
    out = set()
    for p in PATS:
        out |= set(re.findall(p, blob))
        # UTF-16LE: decode the UTF-16 runs and search them as text
    for run in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", blob):
        txt = run.decode("utf-16le").encode("latin1")
        for p in PATS:
            out |= set(re.findall(p, txt))
    return {n for n in out if n.isascii() and n not in (b"Public", b"Default", b"All Users")}


def fake(u: bytes) -> bytes:
    return bytes((ord("a") + (b * 31 + i * 7) % 26) for i, b in enumerate(u))


def deflate_hits(data: bytes) -> int:
    """How many zlib/raw-deflate payloads that decode to text with paths are inside this stream?"""
    hits = 0
    for m in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", data):
        try:
            dec = zlib.decompressobj().decompress(data[m.start():m.start() + 2_000_000])
            if len(dec) > 64 and find_names(dec):
                hits += 1
        except zlib.error:
            pass
    return hits


def props(ole: olefile.OleFileIO) -> dict:
    try:
        m = ole.get_metadata()
        return {k: getattr(m, k) for k in ("author", "last_saved_by", "company", "title", "manager") if getattr(m, k, None)}
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)}


def run(path: Path) -> dict:
    ole = olefile.OleFileIO(str(path))
    streams = ole.listdir(streams=True, storages=False)
    names, per_stream, compressed = set(), {}, 0
    meta = props(ole)
    for s in streams:
        data = ole.openstream(s).read()
        n = find_names(data)
        if n:
            per_stream["/".join(s)] = sorted(x.decode() for x in n)
        names |= n
        compressed += deflate_hits(data)
    for v in meta.values():
        if isinstance(v, bytes) and len(v) >= 3:
            names.add(v.strip(b"\x00"))
    sizes_before = {"/".join(s): ole.get_size("/".join(s)) for s in streams}
    ole.close()

    out = path.with_suffix(path.suffix + ".deid")
    shutil.copy(path, out)
    mapping = {n: fake(n) for n in names if n}
    ole_w = olefile.OleFileIO(str(out), write_mode=True)
    changed = 0
    for s in streams:
        data = ole_w.openstream(s).read()
        new = data
        for k, v in mapping.items():
            new = new.replace(k, v).replace(utf16(k), utf16(v))
        if new != data:
            ole_w.write_stream(s, new)
            changed += 1
    ole_w.close()

    ole2 = olefile.OleFileIO(str(out))
    streams2 = ole2.listdir(streams=True, storages=False)
    sizes_after = {"/".join(s): ole2.get_size("/".join(s)) for s in streams2}
    remaining = 0
    for s in streams2:
        data = ole2.openstream(s).read()
        remaining += sum(data.count(k) + data.count(utf16(k)) for k in mapping)
    meta2 = props(ole2)
    ole2.close()
    raw_remaining = sum(out.read_bytes().count(k) + out.read_bytes().count(utf16(k)) for k in mapping)
    return dict(file=path.name, streams=len(streams), names=sorted(x.decode("latin1") for x in names),
                streams_with_names=len(per_stream), sample_streams=list(per_stream)[:6], meta=meta,
                names_in_deflate_payloads=compressed, changed_streams=changed,
                same_streams=streams == streams2, same_sizes=sizes_before == sizes_after,
                remaining_in_streams=remaining, remaining_raw_bytes=raw_remaining, meta_after=meta2)


if __name__ == "__main__":
    import json
    for f in sys.argv[1:]:
        try:
            print(json.dumps(run(Path(f)), default=lambda b: b.decode("latin1", "replace") if isinstance(b, bytes) else str(b),
                             ensure_ascii=False)[:900])
        except Exception as e:  # noqa: BLE001
            print(f, "ERROR", repr(e))
