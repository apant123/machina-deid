"""Audit: where did the spike's raw substring replacement hit inside larger tokens?"""
import re, sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, str(Path(__file__).parent))
from sw_rewrite import parse
from sw_batch import PAT
ALNUM = re.compile(rb"[A-Za-z0-9]")
CLASS_TOK = re.compile(rb"[a-z]{2}[A-Z][A-Za-z0-9]*_c")
def utf16(b): return b.decode("latin1").encode("utf-16le")
def classify(data, k):
    """Yield 'class' | 'inside-token' | 'ok' for every occurrence of k (ASCII + UTF-16)."""
    for enc, step in ((k, 1), (utf16(k), 2)):
        for m in re.finditer(re.escape(enc), data):
            s, e = m.start(), m.end()
            # widen to the surrounding token (ASCII view)
            ctx = data[max(0, s - 40):e + 40]
            if step == 1 and any(c.start() <= s - max(0, s - 40) and c.end() >= e - max(0, s - 40) for c in CLASS_TOK.finditer(ctx)):
                yield "class-name"; continue
            before = data[s - step:s - step + 1] if s >= step else b""
            after = data[e:e + 1]
            yield "inside-token" if (ALNUM.fullmatch(before or b" ") or ALNUM.fullmatch(after or b" ")) else "ok"
files = [p for d in sys.argv[1:] for p in sorted(Path(d).iterdir()) if p.suffix.lower().startswith(".sld")]
per_file = {}; generic = Counter()
for f in files:
    secs = parse(f.read_bytes()); names = set()
    for s in secs:
        for p in PAT: names |= {m for m in re.findall(p, s["data"]) if m.isascii()}
    names = {n for n in names if n not in (b"Public", b"Default", b"All Users")}
    c = Counter()
    for n in names:
        for s in secs:
            for kind in classify(s["data"], n): c[(n.decode(), kind)] += 1
    bad = {k: v for k, v in c.items() if k[1] != "ok"}
    if bad: per_file[f.name] = bad
    for n in names:
        if n.lower() in (b"user", b"admin", b"administrator", b"owner", b"pc", b"guest", b"solidworks"): generic[n.decode()] += 1
print(f"files scanned: {len(files)} | files with non-boundary hits: {len(per_file)}")
kinds = Counter(k[1] for v in per_file.values() for k in v)
print("hit kinds across those files:", dict(kinds))
print("generic account names extracted as 'people':", dict(generic))
for f, v in list(per_file.items())[:12]:
    print(" ", f, {f"{n}/{k}": c for (n, k), c in v.items()})
