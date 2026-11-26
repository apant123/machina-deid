"""Spike: reverse-engineer the SolidWorks 2015+ container (exploratory)."""
import sys, zlib, struct
from pathlib import Path
MARK = b"\x14\x00\x06\x00\x08\x00"
def nib(b): return bytes(((x << 4) & 0xF0) | (x >> 4) for x in b)

def sections(data):
    pos = data.find(MARK)
    out = []
    while pos != -1:
        h = pos + len(MARK)
        x, a, b, c, nlen = struct.unpack_from("<IIIII", data, h)
        name = nib(data[h + 20:h + 20 + nlen]) if nlen < 512 else b"?"
        dstart = h + 20 + nlen
        out.append(dict(pos=pos, x=x, a=a, b=b, c=c, nlen=nlen, name=name, dstart=dstart))
        pos = data.find(MARK, pos + 1)
    return out

for f in sys.argv[1:]:
    d = Path(f).read_bytes()
    secs = sections(d)
    print(f"\n### {Path(f).name} size={len(d)} first4={d[:4].hex()} sections={len(secs)} first_marker_at={secs[0]['pos'] if secs else None}")
    print("   bytes between 00000004 and first marker:", d[8:secs[0]['pos']].hex() if secs else "")
    for i, s in enumerate(secs[:8]):
        nxt = secs[i + 1]["pos"] if i + 1 < len(secs) else len(d)
        span = nxt - s["dstart"]
        dec = None
        try:
            do = zlib.decompressobj(-15); dec = do.decompress(d[s["dstart"]:nxt]); used = span - len(do.unused_data)
        except Exception as e:
            used = f"ERR {e}"
        crc = zlib.crc32(dec) if dec is not None else None
        print(f"  @{s['pos']:7d} name={s['name'][:40]!r} x={s['x']:#010x} a={s['a']} b={s['b']} c={s['c']} span={span} deflate_used={used} out={len(dec) if dec is not None else None} crc_match_x={crc==s['x']}")
