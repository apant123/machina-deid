"""Stage 1 runner: harvest strings from every unique blob and unpacked archive member.

    python -m deid.harvest_run jobs                 # build harvest/jobs.parquet
    python -m deid.harvest_run run [--workers 15] [--limit N] [--formats step,pdf]
    python -m deid.harvest_run paths                # path/key components (no download)
    python -m deid.harvest_run aggregate            # -> strings.parquet + summary

Each unique sha256 is processed once (blobs: staging canonical key; members:
`_unpacked/...`). Output parts are written every few thousand files; reruns skip
sha256s already present in a status part (resumable after SSO expiry / crash).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from multiprocessing import get_context
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .config import DATA_DIR, MANIFEST_DIR, STAGING_BUCKET, s3_client
from .harvest import ARCHIVE_FORMATS, harvest_file

HARVEST_DIR = DATA_DIR / "harvest"
PARTS = HARVEST_DIR / "parts"
TMP = DATA_DIR / "tmp_harvest"
SKIP_FORMATS = ARCHIVE_FORMATS | {"empty", "partial-download"}

STR_SCHEMA = pa.schema([("sha256", pa.string()), ("kind", pa.string()), ("value", pa.string()), ("count", pa.int64())])
STATUS_SCHEMA = pa.schema([("sha256", pa.string()), ("format", pa.string()), ("size", pa.int64()),
                           ("status", pa.string()), ("error", pa.string()), ("seconds", pa.float64()),
                           ("n_unique", pa.int64()), ("images", pa.int64()), ("scanned_pages", pa.int64()),
                           ("truncated", pa.bool_()), ("notes", pa.string())])


# --------------------------------------------------------------------------- jobs
def build_jobs() -> pa.Table:
    blobs = pq.read_table(MANIFEST_DIR / "blobs.parquet", columns=["sha256", "size", "format", "canonical_key"])
    members = pq.read_table(MANIFEST_DIR / "members.parquet", columns=["sha256", "size", "format", "s3_key", "is_archive"])
    rows, seen = [], set()
    for r in blobs.to_pylist():
        if r["sha256"] and r["format"] not in SKIP_FORMATS:
            seen.add(r["sha256"])
            rows.append(dict(sha256=r["sha256"], size=r["size"], format=r["format"], s3_key=r["canonical_key"], origin="blob"))
        elif r["sha256"]:
            seen.add(r["sha256"])
    for r in members.to_pylist():
        if r["sha256"] in seen or r["is_archive"] or r["format"] in SKIP_FORMATS:
            continue
        seen.add(r["sha256"])
        rows.append(dict(sha256=r["sha256"], size=r["size"], format=r["format"], s3_key=r["s3_key"], origin="member"))
    t = pa.Table.from_pylist(rows)
    HARVEST_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, HARVEST_DIR / "jobs.parquet")
    return t


# --------------------------------------------------------------------------- worker
_s3 = None


class _Timeout(Exception):
    pass


def _alarm(_sig, _frm):
    raise _Timeout()


def _init():
    global _s3
    _s3 = s3_client()
    signal.signal(signal.SIGALRM, _alarm)


def work(job: dict):
    t0 = time.time()
    st = dict(sha256=job["sha256"], format=job["format"], size=job["size"], status="ok", error=None,
              n_unique=0, images=0, scanned_pages=0, truncated=False, notes=None)
    rows = []
    fd, tmp = tempfile.mkstemp(dir=TMP, suffix="." + job["format"].split(":")[-1][:10])
    os.close(fd)
    try:
        _s3.download_file(STAGING_BUCKET, job["s3_key"], tmp)
        scale = float(os.environ.get("HARVEST_TIMEOUT_SCALE", "1"))
        signal.alarm(int((180 + job["size"] / 2_000_000) * scale))  # ~2 MB/s floor
        try:
            h = harvest_file(tmp, job["format"])
        finally:
            signal.alarm(0)
        rows = [(job["sha256"], k, v, n) for (k, v), n in h.strings.items()]
        st.update(n_unique=len(rows), images=h.images, scanned_pages=h.scanned_pages, truncated=h.truncated,
                  notes="; ".join(h.notes)[:1000] or None)
    except _Timeout:
        st.update(status="timeout")
    except Exception as e:  # noqa: BLE001
        msg = repr(e)
        st.update(status="auth_error" if "Token" in msg or "ExpiredToken" in msg or "credentials" in msg.lower() else "error",
                  error=(msg + " | " + traceback.format_exc(limit=3))[-800:])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    st["seconds"] = round(time.time() - t0, 3)
    return st, rows


# --------------------------------------------------------------------------- run
def done_shas(retry_failed: bool = False) -> set[str]:
    out = set()
    keep = ["ok"] if retry_failed else ["ok", "timeout", "error"]
    for p in PARTS.glob("status-*.parquet"):
        t = pq.read_table(p, columns=["sha256", "status"])
        ok = pc.is_in(t["status"], pa.array(keep))
        out |= set(t.filter(ok)["sha256"].to_pylist())
    return out


def _flush(strings: list, statuses: list) -> None:
    if not statuses:
        return
    n = len(list(PARTS.glob("status-*.parquet")))
    tag = f"{n:05d}-{os.getpid()}-{int(time.time())}"
    good = {s["sha256"] for s in statuses if s["status"] != "auth_error"}
    pq.write_table(pa.Table.from_pylist([s for s in statuses if s["status"] != "auth_error"], STATUS_SCHEMA),
                   PARTS / f"status-{tag}.parquet")
    rows = [r for r in strings if r[0] in good]
    cols = list(zip(*rows)) if rows else [[], [], [], []]
    pq.write_table(pa.table([pa.array(c, t.type) for c, t in zip(cols, STR_SCHEMA)], schema=STR_SCHEMA),
                   PARTS / f"strings-{tag}.parquet", compression="zstd")


def ensure_display() -> None:
    """ODA File Converter (DWG) needs an X display; start one shared Xvfb."""
    if os.environ.get("DISPLAY"):
        return
    os.environ["DISPLAY"] = ":99"
    if subprocess.run(["pgrep", "-f", "Xvfb :99"], capture_output=True).returncode != 0:
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1)


def run(workers: int, limit: int | None, formats: set[str] | None, flush_every: int = 3000,
        retry_failed: bool = False) -> None:
    PARTS.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    jobs_path = HARVEST_DIR / "jobs.parquet"
    jobs = (pq.read_table(jobs_path) if jobs_path.exists() else build_jobs()).to_pylist()
    done = done_shas(retry_failed)
    if retry_failed:
        os.environ.setdefault("HARVEST_TIMEOUT_SCALE", "6")
    todo = [j for j in jobs if j["sha256"] not in done and (not formats or j["format"] in formats)]
    todo.sort(key=lambda j: -j["size"])  # big files first -> better tail behaviour
    if limit:
        todo = todo[:limit]
    total_bytes = sum(j["size"] for j in todo)
    print(f"jobs={len(jobs)} done={len(done)} todo={len(todo)} bytes={total_bytes/1e9:.1f} GB", flush=True)
    if any(j["format"] == "dwg" for j in todo):
        ensure_display()
    strings, statuses, by_status = [], [], Counter()
    t0, nbytes, auth_err = time.time(), 0, 0
    ctx = get_context("fork")
    with ctx.Pool(workers, initializer=_init, maxtasksperchild=300) as pool:
        for i, (st, rows) in enumerate(pool.imap_unordered(work, todo, chunksize=1), 1):
            statuses.append(st)
            strings.extend(rows)
            by_status[st["status"]] += 1
            nbytes += st["size"]
            if st["status"] == "auth_error":
                auth_err += 1
                if auth_err >= 20:
                    print("AWS credentials expired: stopping (rerun after `aws sso login --profile machina`)", flush=True)
                    pool.terminate()
                    break
            if len(statuses) >= flush_every or len(strings) >= 3_000_000:
                _flush(strings, statuses)
                strings, statuses = [], []
            if i % 500 == 0:
                el = time.time() - t0
                print(f"{i}/{len(todo)} {nbytes/1e9:.1f}/{total_bytes/1e9:.1f} GB {el/60:.1f} min "
                      f"{dict(by_status)}", flush=True)
    _flush(strings, statuses)
    print(f"finished {dict(by_status)} in {(time.time()-t0)/60:.1f} min", flush=True)


# --------------------------------------------------------------------------- paths
def harvest_paths() -> None:
    """Key components of every object and archive member -> path strings (no download)."""
    man = pq.read_table(MANIFEST_DIR / "manifest.parquet", columns=["key", "sha256", "top_prefix", "owner", "repo"]).to_pylist()
    mem = pq.read_table(MANIFEST_DIR / "members.parquet", columns=["archive_sha256", "member_path", "sha256"]).to_pylist()
    c = Counter()
    for r in man:
        parts = r["key"].split("/")[1:]  # drop 'sources'
        if r["owner"]:
            c[("path.owner", r["owner"])] += 1
        if r["repo"]:
            c[("path.repo", r["repo"])] += 1
        for p in parts[1:-1]:
            if p not in (r["owner"], r["repo"]) and not p.endswith(".extracted"):
                c[("path.dir", p)] += 1
            elif p.endswith(".extracted"):
                c[("path.dir", p[: -len(".extracted")])] += 1
        c[("path.file", parts[-1])] += 1
    for r in mem:
        parts = r["member_path"].split("/")
        for p in parts[:-1]:
            c[("member.dir", p)] += 1
        c[("member.file", parts[-1])] += 1
    rows = [dict(kind=k, value=v, count=n) for (k, v), n in c.items() if v]
    HARVEST_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), HARVEST_DIR / "paths.parquet", compression="zstd")
    print(f"path strings: {len(rows)} unique ({Counter(r['kind'] for r in rows)})")


# --------------------------------------------------------------------------- aggregate
def aggregate() -> None:
    """117M occurrence rows: aggregate out-of-core with DuckDB."""
    import duckdb
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{HARVEST_DIR / 'duck_tmp'}'; SET memory_limit='16GB'; SET threads=14")
    P, H = str(PARTS), str(HARVEST_DIR)
    # a retried file has several status rows: keep the best (ok > timeout/error)
    con.execute(f"""COPY (SELECT * FROM read_parquet('{P}/status-*.parquet', filename=true)
                    QUALIFY row_number() OVER (PARTITION BY sha256 ORDER BY (status='ok') DESC, filename DESC)=1
                    ORDER BY sha256) TO '{H}/status.parquet' (FORMAT parquet)""")
    con.execute(f"COPY (SELECT * EXCLUDE (filename) FROM read_parquet('{H}/status.parquet')) TO '{H}/status2.parquet' (FORMAT parquet)")
    os.replace(f"{H}/status2.parquet", f"{H}/status.parquet")
    con.execute(f"COPY (SELECT * FROM read_parquet('{P}/strings-*.parquet')) TO '{H}/occurrences.parquet' (FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"""COPY (SELECT kind, value, sum(count) AS count, count(DISTINCT sha256) AS n_blobs
                    FROM read_parquet('{H}/occurrences.parquet') GROUP BY kind, value)
                    TO '{H}/strings.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    con.execute(f"""COPY (SELECT value, count(DISTINCT sha256) AS n_blobs, sum(count) AS count, list(DISTINCT kind) AS kinds
                    FROM read_parquet('{H}/occurrences.parquet') GROUP BY value)
                    TO '{H}/values.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    st = f"read_parquet('{H}/status.parquet')"
    by_fmt = {r[0]: dict(files=r[1], ok=r[2], bytes=r[3], seconds=round(r[4], 1), images=r[5], scanned_pages=r[6],
                         truncated=r[7], unique_strings=r[8])
              for r in q(f"""SELECT format, count(*), count(*) FILTER (status='ok'), sum(size), sum(seconds), sum(images),
                              sum(scanned_pages), count(*) FILTER (truncated), sum(n_unique) FROM {st}
                              GROUP BY 1 ORDER BY 2 DESC""")}
    kinds = q(f"""SELECT split_part(kind,'.',1)||'.'||split_part(kind,'.',2) k, count(*) n
                  FROM read_parquet('{H}/strings.parquet') GROUP BY 1 ORDER BY 2 DESC LIMIT 80""")
    tot = q(f"SELECT count(*), sum(images), sum(scanned_pages), sum(seconds) FROM {st}")[0]
    summary = dict(
        files=tot[0], status=dict(q(f"SELECT status, count(*) FROM {st} GROUP BY 1")),
        occurrence_rows=q(f"SELECT count(*) FROM read_parquet('{H}/occurrences.parquet')")[0][0],
        unique_kind_value=q(f"SELECT count(*) FROM read_parquet('{H}/strings.parquet')")[0][0],
        unique_values=q(f"SELECT count(*) FROM read_parquet('{H}/values.parquet')")[0][0],
        images_total=tot[1], scanned_pdf_pages=tot[2], cpu_hours=round(tot[3] / 3600, 2),
        by_format=by_fmt, top_kind_groups=dict(kinds),
        failures=[dict(sha256=r[0], format=r[1], status=r[2], error=(r[3] or "")[:300])
                  for r in q(f"SELECT sha256, format, status, error FROM {st} WHERE status<>'ok'")],
        truncated=[dict(sha256=r[0], format=r[1]) for r in q(f"SELECT sha256, format FROM {st} WHERE truncated")][:200],
    )
    (HARVEST_DIR / "harvest_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({k: summary[k] for k in ("files", "status", "occurrence_rows", "unique_kind_value",
                                              "unique_values", "images_total", "scanned_pdf_pages", "cpu_hours")}))


def upload() -> None:
    s3 = s3_client()
    for p in [*HARVEST_DIR.glob("*.parquet"), HARVEST_DIR / "harvest_summary.json"]:
        if p.exists():
            s3.upload_file(str(p), STAGING_BUCKET, f"_harvest/{p.name}")
            print("uploaded", p.name)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["jobs", "run", "paths", "aggregate", "upload"])
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--formats")
    ap.add_argument("--retry-failed", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "jobs":
        t = build_jobs()
        print(t.num_rows, "jobs", sum(t["size"].to_pylist()) / 1e9, "GB")
        print(Counter(t["format"].to_pylist()).most_common(40))
    elif a.cmd == "run":
        run(a.workers, a.limit, set(a.formats.split(",")) if a.formats else None, retry_failed=a.retry_failed)
    elif a.cmd == "paths":
        harvest_paths()
    elif a.cmd == "aggregate":
        aggregate()
    else:
        upload()


if __name__ == "__main__":
    sys.exit(main())
