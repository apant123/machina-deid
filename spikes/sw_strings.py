import sys, zlib, struct, re
from pathlib import Path
MARK = b"\x14\x00\x06\x00\x08\x00"
def nib(b): return bytes(((x << 4) & 0xF0) | (x >> 4) for x in b)
SKIP = ("Preview", "PreviewPNG", "Contents/DisplayLists", "Contents/Config-0-Partition", "Contents/Config-0-GhostPartition")
for f in sys.argv[1:]:
    d = Path(f).read_bytes(); pos = d.find(MARK); print("\n####", Path(f).name)
    while pos != -1:
        h = pos + 6; x, a, b, c, nlen = struct.unpack_from("<IIIII", d, h); ds = h + 20 + nlen
        if 0 < b <= len(d) - ds and nlen < 400:
            try:
                do = zlib.decompressobj(-15); dec = do.decompress(d[ds:ds + b])
                if do.eof and len(dec) == c and zlib.crc32(dec) == a:
                    name = nib(d[h + 20:ds]).decode("latin1")
                    if name not in SKIP:
                        s = [m.decode("latin1") for m in re.findall(rb"[\x20-\x7e]{5,}", dec)]
                        s += [m.decode("utf-16le") for m in re.findall(rb"(?:[\x20-\x7e]\x00){5,}", dec)]
                        s = [x for x in s if re.search(r"[A-Za-z]{3}", x) and not x.startswith("mo") and "_c" not in x]
                        if s: print(f"  [{name}] {s[:12]}")
            except zlib.error: pass
        pos = d.find(MARK, pos + 1)
