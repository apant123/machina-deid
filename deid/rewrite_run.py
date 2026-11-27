"""Stage 3 runner: de-identify every unique blob once and write it to every (renamed) key.

    python -m deid.rewrite_run plan                          # renamed keys + collision check -> rewrite/keys.parquet
    python -m deid.rewrite_run run [--workers 14] [--limit N] [--formats pdf,step] [--dry-run]
    python -m deid.rewrite_run report                        # -> rewrite/summary.json (+ quarantine list)

Input: staged blobs (`s3://<staging>/<key>`, deduplicated by sha256 in manifest/blobs.parquet).
Archives are rewritten as a whole (members recursively); `.extracted/` siblings are ordinary blobs.
Output: `s3://<output>/<renamed key>`; quarantined blobs are NOT written (listed in the report).
Resumable: status parts are committed every few hundred blobs; reruns skip finished sha256s.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import time
import traceback
from collections import Counter
from multiprocessing import get_context
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .config import DATA_DIR, MANIFEST_DIR, OUTPUT_BUCKET, STAGING_BUCKET, s3_client

RW_DIR = DATA_DIR / "rewrite"
PARTS = RW_DIR / "parts"
TMP = DATA_DIR / "tmp_rewrite"
TIMEOUT = 3600
SCHEMA = pa.schema([("sha256", pa.string()), ("format", pa.string()), ("size", pa.int64()), ("status", pa.string()),
                    ("out_fmt", pa.string()), ("out_size", pa.int64()), ("seconds", pa.float64()), ("n_keys", pa.int64()),
                    ("members", pa.int64()), ("residual", pa.string()), ("info", pa.string()), ("error", pa.string())])


# ---------------------------------------------------------------- plan
def plan() -> None:
    from .rewrite.mapping import DeidMap
    dm = DeidMap.load(with_refs=False)
    b = pq.read_table(MANIFEST_DIR / "blobs.parquet", columns=["sha256", "keys"]).to_pylist()
    rows, seen = [], {}
    collisions = []
    for r in b:
        for k in r["keys"]:
            nk = dm.key(k)
            if nk in seen and seen[nk] != k:
                collisions.append((seen[nk], k, nk))
            seen[nk] = k
            rows.append(dict(sha256=r["sha256"], key=k, new_key=nk, renamed=nk != k))
    RW_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), RW_DIR / "keys.parquet")
    print(dict(keys=len(rows), renamed=sum(r["renamed"] for r in rows), collisions=len(collisions)))
    if collisions:
        (RW_DIR / "collisions.json").write_text(json.dumps(collisions[:1000], ensure_ascii=False, indent=1))
        raise SystemExit("key collisions: see rewrite/collisions.json")


# ---------------------------------------------------------------- worker
_ctx = None
_s3 = None
_keys: dict = {}
_dry = False


class _Timeout(Exception):
    pass


def _alarm(_s, _f):
    raise _Timeout()


def _init(dry: bool):
    global _ctx, _s3, _keys, _dry
    import warnings
    warnings.filterwarnings("ignore")
    from .logo_scan import load_cache
    from .logo_clip import load_blank_set
    from .rewrite.dispatch import Ctx
    from .rewrite.mapping import default_map
    TMP.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DEID_TMP", str(TMP))
    from .vector_logo_scan import confirmed
    _ctx = Ctx(default_map(), tmp=TMP, logo_cache=load_cache(), blank_images=load_blank_set(), vector_logos=confirmed())
    _s3 = s3_client()
    _dry = dry
    signal.signal(signal.SIGALRM, _alarm)


def _upload(path: Path, keys: list[str]) -> None:
    from boto3.s3.transfer import TransferConfig
    cfg = TransferConfig(multipart_threshold=64 << 20, multipart_chunksize=64 << 20, max_concurrency=4)
    first = keys[0]
    _s3.upload_file(str(path), OUTPUT_BUCKET, first, Config=cfg)
    for k in keys[1:]:  # duplicates: server-side copy
        _s3.copy({"Bucket": OUTPUT_BUCKET, "Key": first}, OUTPUT_BUCKET, k, Config=cfg)


def work(job: dict) -> dict:
    from .rewrite.dispatch import rewrite_path
    from .replace import Stats
    t0 = time.time()
    out = dict(sha256=job["sha256"], format=job["format"], size=job["size"], status="error", out_fmt=None, out_size=None,
               n_keys=len(job["keys"]), members=0, residual=None, info=None, error=None)
    inflight = TMP / "inflight" / str(os.getpid())  # which blob each worker is on (diagnose OOM-killed workers)
    inflight.parent.mkdir(parents=True, exist_ok=True)
    inflight.write_text(f"{job['sha256']} {job['format']} {job['size']}\n")
    fd, src = tempfile.mkstemp(dir=TMP, prefix="src-")
    os.close(fd)
    dst = Path(src + ".out")
    try:
        signal.alarm(TIMEOUT)
        _s3.download_file(STAGING_BUCKET, job["canonical_key"], src)
        _ctx.st = Stats()
        _ctx.blob_sha = job["sha256"]  # page-region logos are looked up per top-level blob
        name = job["canonical_key"].rsplit("/", 1)[-1]
        o = rewrite_path(Path(src), dst, name, _ctx)
        out.update(status=o.status, out_fmt=o.fmt, members=o.members, residual=json.dumps(o.residual[:50], ensure_ascii=False),
                   info=json.dumps(dict(o.info, quarantined_members=o.quarantined_members[:20], replaced=_ctx.st.replaced),
                                   ensure_ascii=False, default=str)[:20000],
                   error=(o.info or {}).get("error"))
        if o.status in ("ok", "copy"):
            out["out_size"] = dst.stat().st_size
            if not _dry:
                _upload(dst, [_keys_for(job)[k] for k in job["keys"]])
    except _Timeout:
        out.update(status="quarantine", error=f"timeout {TIMEOUT}s")
    except Exception as e:  # noqa: BLE001
        out.update(status="error", error=f"{type(e).__name__}: {e}"[:300], info=traceback.format_exc()[-2000:])
    finally:
        signal.alarm(0)
        for p in (Path(src), dst, inflight):
            p.unlink(missing_ok=True)
    out["seconds"] = round(time.time() - t0, 2)
    return out


def _keys_for(job):
    return job["renamed"]


# ---------------------------------------------------------------- driver
def _done(retry_error: str | None = None) -> set:
    """Blobs with a final status in their latest status part (parts are numbered in write order).

    Errors are always retried; `retry_error` also re-queues quarantines whose error contains it."""
    last = {}
    for p in sorted(PARTS.glob("status-*.parquet")):
        for r in pq.read_table(p, columns=["sha256", "status", "error"]).to_pylist():
            if r["status"] != "error" or r["sha256"] not in last:
                last[r["sha256"]] = r
    return {h for h, r in last.items() if r["status"] != "error" and not
            (retry_error and r["status"] == "quarantine" and retry_error in (r["error"] or ""))}


def run(workers: int, limit: int | None, formats: set | None, dry: bool, retry_error: str | None = None,
        only: set | None = None) -> None:
    """`only`: re-run exactly these blob sha256s (targeted re-run after a map change), done or not."""
    PARTS.mkdir(parents=True, exist_ok=True)
    keys = {}
    for r in pq.read_table(RW_DIR / "keys.parquet").to_pylist():
        keys.setdefault(r["sha256"], {})[r["key"]] = r["new_key"]
    blobs = pq.read_table(MANIFEST_DIR / "blobs.parquet", columns=["sha256", "size", "format", "canonical_key", "keys"]).to_pylist()
    done = set() if only else _done(retry_error)
    jobs = [dict(b, renamed=keys[b["sha256"]]) for b in blobs if b["sha256"] not in done and
            (not only or b["sha256"] in only) and (not formats or b["format"].split(":")[0] in formats)]
    jobs.sort(key=lambda j: -j["size"])  # big files first: the tail of the run is short ones
    if limit:
        jobs = jobs[:limit]
    print(f"rewrite: {len(jobs)} blobs, {sum(j['size'] for j in jobs) / 1e9:.1f} GB, workers={workers}, dry={dry}", flush=True)
    rows, t0, n, nbytes = [], time.time(), 0, 0

    def flush():
        if rows:
            tag = f"{len(list(PARTS.glob('status-*.parquet'))):05d}-{os.getpid()}"  # pid: concurrent runners never collide
            pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), PARTS / f"status-{tag}.parquet")
            rows.clear()
    with get_context("fork").Pool(workers, initializer=_init, initargs=(dry,), maxtasksperchild=200) as pool:
        for r in pool.imap_unordered(work, jobs, chunksize=1):
            rows.append(r)
            n += 1
            nbytes += r["size"] or 0
            if len(rows) >= 300 or n == len(jobs):
                flush()
                el = time.time() - t0
                c = Counter(x["status"] for x in pq.read_table(sorted(PARTS.glob("status-*.parquet"))[-1]).to_pylist())
                print(f"{n}/{len(jobs)} {nbytes / 1e9:.1f} GB {el / 60:.1f} min  {nbytes / 1e6 / el:.1f} MB/s  last part {dict(c)}",
                      flush=True)
    flush()
    print(f"rewrite finished in {(time.time() - t0) / 60:.1f} min", flush=True)


def report() -> None:
    import duckdb
    con = duckdb.connect()
    con.execute(f"CREATE VIEW s AS SELECT * FROM read_parquet('{PARTS}/status-*.parquet', filename=true)")
    # last status per sha (retries)
    con.execute("CREATE VIEW last AS SELECT * FROM s QUALIFY row_number() OVER (PARTITION BY sha256 ORDER BY status='error', filename DESC) = 1")  # latest final status wins
    summ = dict(
        by_status=dict(con.execute("SELECT status, count(*) FROM last GROUP BY 1").fetchall()),
        by_format=[dict(zip(["format", "status", "n", "gb", "keys"], r)) for r in con.execute(
            "SELECT split_part(format, ':', 1), status, count(*), round(sum(size)/1e9, 2), sum(n_keys) FROM last GROUP BY 1, 2 ORDER BY 4 DESC").fetchall()],
        seconds=con.execute("SELECT round(sum(seconds)/3600, 2) FROM last").fetchone()[0],
        residual_files=con.execute("SELECT count(*) FROM last WHERE residual NOT IN ('[]', '') AND residual IS NOT NULL").fetchone()[0],
    )
    q = con.execute("SELECT sha256, format, size, n_keys, error FROM last WHERE status IN ('quarantine', 'error') ORDER BY size DESC").fetchall()
    (RW_DIR / "quarantine.json").write_text(json.dumps([dict(zip(["sha256", "format", "size", "n_keys", "error"], r)) for r in q],
                                                       ensure_ascii=False, indent=1))
    (RW_DIR / "summary.json").write_text(json.dumps(summ, indent=1))
    print(json.dumps(summ, indent=1)[:4000])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "run", "report"])
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--formats")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retry-error", help="also re-run quarantines whose error contains this text")
    ap.add_argument("--only", help="file with blob sha256s (one per line): re-run exactly these")
    a = ap.parse_args()
    if a.cmd == "plan":
        plan()
    elif a.cmd == "run":
        run(a.workers, a.limit, set(a.formats.split(",")) if a.formats else None, a.dry_run, a.retry_error,
            set(Path(a.only).read_text().split()) if a.only else None)
    else:
        report()
    sys.exit(0)
