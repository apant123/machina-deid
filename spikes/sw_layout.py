import sys, zlib, struct
from pathlib import Path
MARK = b"\x14\x00\x06\x00\x08\x00"
def nib(b): return bytes(((x << 4) & 0xF0) | (x >> 4) for x in b)
for f in sys.argv[1:]:
    d = Path(f).read_bytes()
    real, other = [], []
    pos = d.find(MARK)
    while pos != -1:
        h = pos + 6; x, a, b, c, nlen = struct.unpack_from("<IIIII", d, h); ds = h + 20 + nlen
        ok = False
        if 0 < b <= len(d) - ds and nlen < 400:
            try:
                do = zlib.decompressobj(-15); dec = do.decompress(d[ds:ds + b]); ok = do.eof and len(dec) == c and zlib.crc32(dec) == a
            except zlib.error: pass
        (real if ok else other).append((pos, ds + b if ok else None, nlen))
        pos = d.find(MARK, pos + 1)
    # contiguity of real sections
    gaps = [real[i+1][0] - real[i][1] for i in range(len(real)-1)]
    # small "other" entries: where are they?
    print(f"{Path(f).name}: size={len(d)} real={len(real)} other={len(other)} | first real @{real[0][0]} last real ends @{real[-1][1]} | gaps between real: min={min(gaps)} max={max(gaps)} zero-gaps={gaps.count(0)}")
    print("   other entries positions (first 5 / last 5):", [o[0] for o in other[:5]], [o[0] for o in other[-5:]])
    # are real offsets stored anywhere as LE uint32?
    offs = {p for p, _, _ in real}
    hits = sum(1 for i in range(0, len(d) - 3) if struct.unpack_from("<I", d, i)[0] in offs and struct.unpack_from("<I", d, i)[0] > 1000)
    print(f"   LE-uint32 occurrences equal to a real section offset (>1000): {hits}")
    tail = d[real[-1][1]:]
    print(f"   trailer after last real section: {len(tail)} bytes, head={tail[:48].hex()}")
