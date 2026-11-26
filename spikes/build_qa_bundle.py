"""Rebuild the eDrawings QA bundle (v2) with the structure-safe Replacer.

Usage: python spikes/build_qa_bundle.py <v1_dir> <out_dir>
  v1_dir/original/*  source files (01-10 SW 2015+, 11-13 OLE2); v1_dir/manifest.csv for metadata
"""
import csv
import re
import shutil
import sys
from pathlib import Path

import olefile

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from sw_rewrite import parse, rewrite, verify, structural_tokens  # noqa: E402
from sw_batch import extract_names, fake_str, PAT_FULL  # noqa: E402
from deid.replace import Replacer, is_generic_account  # noqa: E402


def ole_names(path: Path) -> set[str]:
    ole = olefile.OleFileIO(str(path))
    names = set()
    for s in ole.listdir(streams=True, storages=False):
        data = ole.openstream(s).read()
        texts = [data] + [r.decode("utf-16-le").encode("latin1", "replace")
                          for r in re.findall(rb"(?:[\x20-\x7e]\x00){6,}", data)]
        for t in texts:
            for p in PAT_FULL:
                for m in re.findall(p, t):
                    n = m.decode("latin1").strip()
                    if n.isascii() and len(n) >= 3 and not is_generic_account(n):
                        names.add(n)
    meta = ole.get_metadata()
    for f in ("author", "last_saved_by"):
        v = getattr(meta, f, None)
        if v:
            n = v.decode("latin1", "replace").strip("\x00 ")
            if n.isascii() and len(n) >= 3 and not is_generic_account(n):
                names.add(n)
    ole.close()
    return names


def build(v1: Path, out: Path) -> list[dict]:
    shutil.rmtree(out, ignore_errors=True)
    (out / "rewritten").mkdir(parents=True)
    shutil.copytree(v1 / "original", out / "original")
    old_rows = {r["file"]: r for r in csv.DictReader(open(v1 / "manifest.csv"))}
    rows = []
    for f in sorted((v1 / "original").iterdir()):
        d = f.read_bytes()
        dst = out / "rewritten" / f.name
        if "_OLE_" not in f.name:
            names = extract_names(parse(d))
            rep = Replacer({n: fake_str(n) for n in names})
            new, st = rewrite(d, rep)
            v = verify(d, new)
            dst.write_bytes(new)
            ok = all(v.values()) and not st["exact_fail"]
            res, n_rep, changed = sorted(st["residual"]), st["replaced"], st["changed"]
        else:
            names = ole_names(f)
            rep = Replacer({n: fake_str(n) for n in names})
            shutil.copy(f, dst)
            ole = olefile.OleFileIO(str(dst), write_mode=True)
            streams = ole.listdir(streams=True, storages=False)
            n_rep = changed = 0
            res, struct_ok = set(), True
            for s in streams:
                data = ole.openstream(s).read()
                new, st = rep.apply(data)
                res |= set(rep.residual(new))
                n_rep += st.replaced
                struct_ok &= structural_tokens(data) == structural_tokens(new)
                if new != data:
                    ole.write_stream(s, new)
                    changed += 1
            ole.close()
            o2 = olefile.OleFileIO(str(dst))
            ok = struct_ok and o2.listdir(streams=True, storages=False) == streams
            o2.close()
            res = sorted(res)
        old = old_rows[f.name]
        rows.append(dict(file=f.name, type=old["type"], sw_version_hint=old["sw_version_hint"],
                         v1_result="FAIL (parsing exception)" if f.name.startswith("10_") else "PASS",
                         sections_or_streams_rewritten=changed, replacements=n_rep, verified_structure_ok=ok,
                         names_replaced="; ".join(f"{n} -> {fake_str(n)}" for n in sorted(names)),
                         dropped_generic=", ".join(rep.dropped_generic), residual_flags=", ".join(res),
                         source_key=old["source_key"]))
    with open(out / "manifest.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return rows


if __name__ == "__main__":
    for r in build(Path(sys.argv[1]), Path(sys.argv[2])):
        print(f"{r['file']:34s} ok={r['verified_structure_ok']} rewritten={r['sections_or_streams_rewritten']:3d} "
              f"repl={r['replacements']:5d} dropped_generic=[{r['dropped_generic']}] residual=[{r['residual_flags']}]")
