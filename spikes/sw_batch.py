"""Batch test of the SW 2015+ rewriter with the structure-safe Replacer.

Spike-level entity extraction (Stage 2 will replace this): usernames in profile
paths + full author / creator / last-saved-by values. Generic accounts dropped.
Per file: exact-size fit, layout, CRCs, structural tokens preserved, residual names.
"""
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from sw_rewrite import parse, rewrite, verify  # noqa: E402
from deid.replace import Replacer, is_generic_account  # noqa: E402

# (kept for audit_substring.py compatibility)
PAT = [rb"\\Users\\([^\\\x00]{3,30})\\", rb"lastModifiedBy>([^<]{3,40})<", rb"<dc:creator>([^<]{3,40})<",
       rb"Documents and Settings\\([^\\\x00]{3,30})\\"]
PAT_FULL = PAT + [rb'name="(?:SW-)?Author"[^>]*><vt:lpstr>([^<]{3,60})<',
                  rb'name="(?:SW-)?Last Saved By"[^>]*><vt:lpstr>([^<]{3,60})<']


def fake(u: bytes) -> bytes:  # byte version, used by older spike scripts
    return bytes((ord("a") + (b * 31 + i * 7) % 26) for i, b in enumerate(u))


def fake_str(name: str) -> str:
    """Same-length ASCII pseudonym preserving case/digits/separators (spike only)."""
    out = []
    for i, ch in enumerate(name):
        h = (ord(ch) * 31 + i * 7 + len(name)) % 26
        if ch.isdigit():
            out.append(str(h % 10))
        elif ch.isalpha() and ch.isascii():
            out.append(chr((65 if ch.isupper() else 97) + h))
        else:
            out.append(ch)
    return "".join(out)


def extract_names(secs) -> set[str]:
    names = set()
    for s in secs:
        for p in PAT_FULL:
            for m in re.findall(p, s["data"]):
                try:
                    n = m.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if n.isascii() and len(n) >= 3 and not is_generic_account(n):
                    names.add(n)
        for run in re.findall(rb"(?:[\x20-\x7e]\x00){6,}", s["data"]):  # UTF-16 profile paths
            t = run.decode("utf-16-le").encode()
            for p in PAT[:1] + PAT[3:]:
                names |= {m.decode() for m in re.findall(p, t) if m.isascii() and not is_generic_account(m.decode())}
    return names


def one(f):
    t0 = time.time()
    d = Path(f).read_bytes()
    try:
        secs = parse(d)
        names = extract_names(secs)
        rep = Replacer({n: fake_str(n) for n in names})
        new, st = rewrite(d, rep)
        v = verify(d, new)
        return dict(f=Path(f).name, size=len(d), names=sorted(names), changed=st["changed"],
                    replaced=st["replaced"], skipped_inside=st["skipped_inside_token"],
                    skipped_structural=st["skipped_structural"], residual=sorted(st["residual"]),
                    exact_fail=st["exact_fail"], verify=v, ok=all(v.values()), secs=round(time.time() - t0, 2),
                    new=new)
    except Exception as e:  # noqa: BLE001
        return dict(f=Path(f).name, error=repr(e)[:300])


if __name__ == "__main__":
    files = [p for d in sys.argv[1:] for p in sorted(Path(d).iterdir()) if p.suffix.lower().startswith(".sld")]
    with ProcessPoolExecutor(16) as ex:
        rs = list(ex.map(one, files))
    ok = [r for r in rs if "error" not in r]
    sw = [r for r in ok if r["verify"]["same_section_count"] and r["changed"] or r["names"]]
    print(json.dumps(dict(
        files=len(rs), errors=[r for r in rs if "error" in r][:3],
        files_with_names=sum(bool(r["names"]) for r in ok),
        sections_changed=sum(r["changed"] for r in ok), replacements=sum(r["replaced"] for r in ok),
        skipped_structural=sum(r["skipped_structural"] for r in ok),
        skipped_inside_token=sum(r["skipped_inside"] for r in ok),
        structure_broken=[r["f"] for r in ok if not r["verify"]["structure_preserved"]],
        verify_fail=[r["f"] for r in ok if not r["ok"]],
        exact_fail_files=[r["f"] for r in ok if r["exact_fail"]],
        files_with_residual=len([r for r in ok if r["residual"]]),
        residual_samples={r["f"]: r["residual"] for r in ok if r["residual"]},
        total_secs=round(sum(r["secs"] for r in ok), 1)), indent=1))
