"""Spike: are SW section field `a` / file first-4-bytes checksums?"""
import sys, zlib, struct, binascii
from pathlib import Path
MARK = b"\x14\x00\x06\x00\x08\x00"
def nib(b): return bytes(((x << 4) & 0xF0) | (x >> 4) for x in b)
for f in sys.argv[1:]:
    d = Path(f).read_bytes()
    pos = d.find(MARK); good = 0; crc_a = adl_a = crc_a_comp = 0; total_real = 0; names = []
    while pos != -1:
        h = pos + 6
        x, a, b, c, nlen = struct.unpack_from("<IIIII", d, h)
        ds = h + 20 + nlen
        if 0 < b <= len(d) - ds and nlen < 400:
            try:
                do = zlib.decompressobj(-15); dec = do.decompress(d[ds:ds + b])
                if do.eof and len(dec) == c:
                    total_real += 1
                    crc_a += zlib.crc32(dec) == a
                    adl_a += zlib.adler32(dec) == a
                    crc_a_comp += zlib.crc32(d[ds:ds + b]) == a
                    names.append(nib(d[h + 20:ds]).decode("latin1"))
            except zlib.error:
                pass
        pos = d.find(MARK, pos + 1)
    first4 = struct.unpack_from("<I", d)[0]; first4be = struct.unpack_from(">I", d)[0]
    cands = {"crc32(d[4:])": zlib.crc32(d[4:]), "crc32(d[8:])": zlib.crc32(d[8:]), "adler(d[4:])": zlib.adler32(d[4:]),
             "adler(d[8:])": zlib.adler32(d[8:]), "crc32(d[8:first_marker])": zlib.crc32(d[8:d.find(MARK)])}
    hit = [k for k, v in cands.items() if v in (first4, first4be)]
    print(f"{Path(f).name}: real_sections={total_real} a==crc32(uncomp)={crc_a} a==adler={adl_a} a==crc32(comp)={crc_a_comp} | first4 checksum hit: {hit or 'none'}")
    print("   section names:", sorted(set(names))[:30])
