"""Stage 2c: count where every map surface occurs in the corpus (precision / recall evidence).

Aho-Corasick over all harvested values (incl. binary text runs), path components and OCR text,
with the same boundary semantics as deid.replace.Replacer:
  global  no ASCII letter/digit directly before/after (CJK keys: plain substring)
  field   the complete value, or the user-name component of a profile path (C:\\Users\\<x>\\, /home/<x>/)
  path    (keys <= 3 chars) only the profile-path user-name component or a whole object-key component
Generated variants that never occur are dropped from the final map.

    python -m deid.entity_scan run --workers 6   # -> entities/scan.parquet, entities/map.parquet
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from multiprocessing import get_context

import pyarrow as pa
import pyarrow.parquet as pq

from .entities import ENT_DIR
from .harvest_run import HARVEST_DIR

_A = None
_KEYS: list = []
_SCOPE: list = []
import re
_PROFILE_ROOT = re.compile(r"(?:[a-z]:[\\/](?:users|documents and settings|docume~1|dokumente und einstellungen|usuarios|"
                           r"utilisateurs|gebruikers|utenti|benutzer)|(?:^|[\s\"'=(:,])/(?:home|users))[\\/]$")


def _edge_ok(c: str) -> bool:
    return not (c.isascii() and c.isalnum())


def _init(keys, scopes):
    global _A, _KEYS, _SCOPE
    import ahocorasick
    _KEYS, _SCOPE = keys, scopes
    _A = ahocorasick.Automaton()
    for i, k in enumerate(keys):
        _A.add_word(k, i)
    _A.make_automaton()


def _scan_values(values, nbs, kinds_list, src):
    hits = defaultdict(lambda: [0, 0, Counter(), []])
    for v, nb, kinds in zip(values, nbs, kinds_list):
        low = v.lower()
        if len(low) != len(v):
            low = v  # rare case-mapping length change: exact-case scan only
        seen = set()
        for end, i in _A.iter(low):
            if i in seen:
                continue
            k = _KEYS[i]
            s = end - len(k) + 1
            before = low[s - 1] if s > 0 else " "
            after = low[end + 1] if end + 1 < len(low) else " "
            if _SCOPE[i] in ("field", "path"):  # complete value (field only), or a profile-path user-name component
                whole = len(k) == len(low.strip()) and (_SCOPE[i] == "field" or src == "path")
                if not (whole or after in "\\/" and _PROFILE_ROOT.search(low, 0, s)):
                    continue
            elif (k[0].isascii() and k[0].isalnum() and not _edge_ok(before)) or \
                    (k[-1].isascii() and k[-1].isalnum() and not _edge_ok(after)):
                continue
            seen.add(i)
            h = hits[i]
            h[0] += 1
            h[1] += nb
            for kd in (kinds or [src])[:3]:
                h[2][kd.split(".")[0] + "." + kd.split(".")[1] if "." in kd else kd] += 1
            if len(h[3]) < 3:
                a = max(0, s - 30)
                h[3].append(v[a:end + 31])
    return {i: (h[0], h[1], dict(h[2].most_common(4)), h[3]) for i, h in hits.items()}


def _work_rg(args):
    path, rg = args
    t = pq.ParquetFile(path).read_row_group(rg, columns=["value", "n_blobs", "kinds"])
    return _scan_values(t["value"].to_pylist(), t["n_blobs"].to_pylist(), t["kinds"].to_pylist(), "values")


def run(workers: int) -> None:
    cand = pq.read_table(ENT_DIR / "map_candidates.parquet").to_pylist()
    keys = [r["surface"].lower() if len(r["surface"].lower()) == len(r["surface"]) else r["surface"] for r in cand]
    scopes = [r["scope"] for r in cand]
    # duplicate lowercase keys (e.g. 'Zephyn' person vs 'zephyn' account) -> first wins
    uniq: dict[str, int] = {}
    for i, k in enumerate(keys):
        uniq.setdefault(k, i)
    ukeys = list(uniq)
    uscopes = [scopes[uniq[k]] for k in ukeys]
    vp = HARVEST_DIR / "values.parquet"
    nrg = pq.ParquetFile(vp).num_row_groups
    total: dict[int, list] = {}

    def merge(res, tag):
        for i, (n, nb, kinds, ex) in res.items():
            t = total.setdefault(i, [0, 0, Counter(), [], Counter()])
            t[0] += n
            t[1] += nb
            t[2].update(kinds)
            t[3].extend(ex[: max(0, 3 - len(t[3]))])
            t[4][tag] += n

    with get_context("fork").Pool(workers, initializer=_init, initargs=(ukeys, uscopes)) as pool:
        for j, res in enumerate(pool.imap_unordered(_work_rg, [(vp, g) for g in range(nrg)]), 1):
            merge(res, "content")
            if j % 20 == 0:
                print(f"{j}/{nrg} row groups", flush=True)
    _init(ukeys, uscopes)
    p = pq.read_table(HARVEST_DIR / "paths.parquet").to_pylist()
    merge(_scan_values([r["value"] for r in p], [r["count"] for r in p], [[r["kind"]] for r in p], "path"), "path")
    import duckdb
    o = duckdb.sql(f"""SELECT value, count(DISTINCT sha256) nb, list(DISTINCT kind) kinds
                       FROM read_parquet('{HARVEST_DIR}/ocr_occurrences.parquet') GROUP BY value""").fetchall()
    merge(_scan_values([r[0] for r in o], [r[1] for r in o], [r[2] for r in o], "ocr"), "ocr")

    idx = {k: j for j, k in enumerate(ukeys)}
    out, keep = [], []
    for r, k in zip(cand, keys):
        j = idx[k]
        t = total.get(j)
        r = dict(r)
        r.update(hit_values=t[0] if t else 0, hit_blobs=t[1] if t else 0,
                 hit_kinds=str(dict(t[2].most_common(4))) if t else "", hit_where=str(dict(t[4])) if t else "",
                 examples=" ¦ ".join(t[3]) if t else "")
        out.append(r)
        if r["observed"] or r["hit_values"]:
            keep.append(r)
    pq.write_table(pa.Table.from_pylist(out), ENT_DIR / "scan.parquet")
    pq.write_table(pa.Table.from_pylist(keep), ENT_DIR / "map.parquet")
    print(dict(candidates=len(out), map=len(keep), with_hits=sum(1 for r in out if r["hit_values"]),
               observed_without_hits=sum(1 for r in out if r["observed"] and not r["hit_values"])))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    run(a.workers)
    sys.exit(0)
