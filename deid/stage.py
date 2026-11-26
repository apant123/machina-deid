"""Stage 0a: stream every R2 source object into the private S3 staging bucket.

One read per byte: each object is streamed from R2, hashed (sha256) and its
leading bytes captured on the fly, while being multipart-uploaded to S3 under
the identical key. The sha256 is also stored as object metadata.

Results are appended to <MANIFEST_DIR>/stage_results.jsonl; re-running skips
keys already recorded as ok, so the job is resumable (e.g. after an SSO
token expiry).

Usage:
    python -m deid.stage [--limit N] [--workers 48] [--prefix sources/catalog/]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from boto3.s3.transfer import TransferConfig

from deid.config import MANIFEST_DIR, R2_BUCKET, R2_PREFIX, STAGING_BUCKET, r2_client, s3_client
from deid.formats import HEAD_BYTES

RESULTS = MANIFEST_DIR / "stage_results.jsonl"
SKIP_EXTS = (".resume-partial",)

XFER = TransferConfig(multipart_threshold=64 * 1024**2, multipart_chunksize=64 * 1024**2,
                      max_concurrency=4, use_threads=True)


class HashingReader:
    """File-like wrapper: hashes bytes and keeps the head as they stream through."""

    def __init__(self, body):
        self._body = body
        self.sha = hashlib.sha256()
        self.head = b""
        self.nbytes = 0

    def read(self, n=-1):
        chunk = self._body.read(n) if n and n > 0 else self._body.read()
        if chunk:
            self.sha.update(chunk)
            if len(self.head) < HEAD_BYTES:
                self.head += chunk[: HEAD_BYTES - len(self.head)]
            self.nbytes += len(chunk)
        return chunk


def list_r2(prefix: str):
    r2 = r2_client()
    for page in r2.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            yield {"key": o["Key"], "size": o["Size"], "r2_etag": o["ETag"].strip('"')}


def load_done() -> set[str]:
    done = set()
    if RESULTS.exists():
        with RESULTS.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("status") == "ok":
                    done.add(r["key"])
    return done


_tls = threading.local()


def _clients():
    if not hasattr(_tls, "r2"):
        _tls.r2, _tls.s3 = r2_client(), s3_client()
    return _tls.r2, _tls.s3


def stage_one(obj: dict) -> dict:
    r2, s3 = _clients()
    key = obj["key"]
    last_err = None
    for attempt in range(4):
        try:
            body = r2.get_object(Bucket=R2_BUCKET, Key=key)["Body"]
            rdr = HashingReader(body)
            # sha256 is only known after streaming, so it goes in the result log
            # (and later onto the object via a metadata copy if needed).
            s3.upload_fileobj(rdr, STAGING_BUCKET, key, Config=XFER)
            if rdr.nbytes != obj["size"]:
                raise IOError(f"size mismatch: got {rdr.nbytes}, expected {obj['size']}")
            return {**obj, "status": "ok", "sha256": rdr.sha.hexdigest(), "head_hex": rdr.head.hex()}
        except Exception as e:  # noqa: BLE001 - log and retry any transfer error
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    return {**obj, "status": "error", "error": last_err}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default=R2_PREFIX)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=48)
    args = ap.parse_args(argv)

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    done = load_done()
    todo = [o for o in list_r2(args.prefix) if o["key"] not in done and not o["key"].endswith(SKIP_EXTS)]
    if args.limit:
        todo = todo[: args.limit]
    total_bytes = sum(o["size"] for o in todo)
    # Largest first so the 1.4 GB files don't become the tail.
    todo.sort(key=lambda o: -o["size"])
    print(f"already done: {len(done):,} | to stage: {len(todo):,} objects, {total_bytes/1e9:.2f} GB", flush=True)

    t0 = time.time()
    n_ok = n_err = 0
    b_done = 0
    lock = threading.Lock()
    with RESULTS.open("a") as out, ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(stage_one, o) for o in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            with lock:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
                out.flush()
            if r["status"] == "ok":
                n_ok += 1
                b_done += r["size"]
            else:
                n_err += 1
                print(f"ERROR {r['key']}: {r['error']}", file=sys.stderr, flush=True)
            if i % 2000 == 0 or i == len(futs):
                dt = time.time() - t0
                print(f"[{dt/60:6.1f} min] {i:,}/{len(futs):,} objs | {b_done/1e9:7.2f}/{total_bytes/1e9:.2f} GB "
                      f"| {b_done/1e6/max(dt,1):6.1f} MB/s | errors {n_err}", flush=True)
    print(f"DONE ok={n_ok:,} errors={n_err:,} in {(time.time()-t0)/60:.1f} min", flush=True)
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
