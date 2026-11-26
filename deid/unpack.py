"""Stage 0c: safely unpack archives that have no `.extracted/` sibling.

Content-addressed: each unique archive blob (sha256) is unpacked once into
    s3://<STAGING_BUCKET>/_unpacked/<archive_sha256>/<member path>
Nested archives found inside are unpacked recursively (up to MAX_DEPTH), each
under its own sha256.

Safety guards (checked *before* bytes are written where the format allows):
  - member paths: no absolute paths, no `..`, no drive letters, no NUL; normalised
  - links/devices: symlinks, hardlinks, device/FIFO entries are skipped (never created)
  - size: per-member cap, per-archive total cap, member-count cap, and a
    compression-ratio cap; streamed decompression enforces caps even when the
    header lies (gzip/Z sizes are unknown up front)
  - depth: nested archives beyond MAX_DEPTH are left packed and flagged
Archives that trip a guard are quarantined (reason recorded), not partially used.

Outputs: <MANIFEST_DIR>/archives.parquet (one row per archive blob)
         <MANIFEST_DIR>/members.parquet  (one row per extracted member)
         both also uploaded to s3://<STAGING_BUCKET>/_manifest/

Usage: python -m deid.unpack [--workers 16] [--limit N]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
import zlib
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

import ncompress
import pyarrow as pa
import pyarrow.parquet as pq

from deid.config import MANIFEST_DIR, STAGING_BUCKET, s3_client
from deid.formats import HEAD_BYTES, detect

MAX_DEPTH = 3
MAX_MEMBERS = 50_000
MAX_MEMBER_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3
MAX_RATIO = 200          # hard cap (bomb); CAD text routinely compresses 10-30x
FLAG_RATIO = 50          # recorded, not rejected
CHUNK = 1024 * 1024

ARCHIVE_FORMATS = {"zip", "zip-other", "7z", "rar", "gzip", "unix-compress", "tar"}
UNPACK_PREFIX = "_unpacked"


class Quarantine(Exception):
    pass


# ---------------------------------------------------------------- path safety
def safe_relpath(name: str) -> str:
    name = name.replace("\\", "/")
    if "\x00" in name:
        raise Quarantine(f"NUL in member name {name!r}")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise Quarantine(f"absolute member path {name!r}")
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise Quarantine(f"path traversal in {name!r}")
    if not parts:
        raise Quarantine(f"empty member name {name!r}")
    return "/".join(parts)


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:  # UTF-8 flag set: zipfile already decoded correctly
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        # Name came from a Unicode-path extra field (0x7075): already decoded properly.
        return info.filename
    for enc in ("utf-8", "gbk", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


class Budget:
    def __init__(self, compressed_size: int):
        self.total = 0
        self.count = 0
        self.compressed = max(compressed_size, 1)

    def add_member(self):
        self.count += 1
        if self.count > MAX_MEMBERS:
            raise Quarantine(f"more than {MAX_MEMBERS} members")

    def add_bytes(self, n: int, member_so_far: int):
        self.total += n
        if member_so_far > MAX_MEMBER_BYTES:
            raise Quarantine(f"member exceeds {MAX_MEMBER_BYTES} bytes")
        if self.total > MAX_TOTAL_BYTES:
            raise Quarantine(f"archive expands beyond {MAX_TOTAL_BYTES} bytes")
        if self.total > 64 * 1024**2 and self.total / self.compressed > MAX_RATIO:
            raise Quarantine(f"compression ratio > {MAX_RATIO}x (possible bomb)")


def copy_capped(src, dst_path: Path, budget: Budget):
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(dst_path, "xb") as out:  # 'x': never overwrite / follow an existing link
        while chunk := src.read(CHUNK):
            n += len(chunk)
            budget.add_bytes(len(chunk), n)
            out.write(chunk)


# ---------------------------------------------------------------- extractors
def extract_zip(path: Path, dest: Path, budget: Budget, notes: list):
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Only trust Unix file-type bits when present; many zips (Windows, writestr)
            # carry permissions only, which must still count as regular files.
            ftype = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
            if ftype and ftype != stat.S_IFREG:
                notes.append(f"skipped non-regular zip entry {info.filename!r}")
                continue
            if info.flag_bits & 0x1:
                raise Quarantine("encrypted zip member")
            rel = safe_relpath(decode_zip_name(info))
            budget.add_member()
            if info.file_size > MAX_MEMBER_BYTES:
                raise Quarantine(f"declared member size {info.file_size} too large")
            with zf.open(info) as src:
                copy_capped(src, dest / rel, budget)


def extract_tar(path: Path, dest: Path, budget: Budget, notes: list):
    with tarfile.open(path, mode="r:*") as tf:
        for m in tf:
            if m.isdir():
                continue
            if not m.isreg():
                notes.append(f"skipped non-regular tar entry {m.name!r} (type {m.type!r})")
                continue
            rel = safe_relpath(m.name)
            budget.add_member()
            src = tf.extractfile(m)
            if src is None:
                continue
            copy_capped(src, dest / rel, budget)


def _single_stream_name(archive_name: str, suffixes: tuple[str, ...]) -> str:
    low = archive_name.lower()
    for s in suffixes:
        if low.endswith(s):
            return archive_name[: -len(s)] or "data"
    return archive_name + ".out"


def extract_gzip(path: Path, dest: Path, budget: Budget, notes: list, name: str):
    out_name = _single_stream_name(name, (".tgz", ".gz", ".z"))
    if name.lower().endswith(".tgz"):
        out_name += ".tar"
    budget.add_member()
    target = dest / safe_relpath(out_name)
    try:
        with gzip.open(path, "rb") as src:
            copy_capped(src, target, budget)
    except (gzip.BadGzipFile, EOFError, zlib.error) as e:
        # Damaged at source (seen: 1990s NIST .arc.gz with CRC/length mismatch).
        # The deflate stream itself is usually intact: recover it raw so its
        # contents still get de-identified, and flag it.
        target.unlink(missing_ok=True)
        notes.append(f"recovered: {type(e).__name__}: {e}")
        recover_raw_gzip(path, target, budget)


def recover_raw_gzip(path: Path, target: Path, budget: Budget):
    """Decode gzip member(s) with raw deflate, ignoring CRC/ISIZE trailers."""
    data = path.read_bytes()
    if len(data) < 18 or data[:2] != b"\x1f\x8b":
        raise ValueError("not a gzip stream")
    flags, pos = data[3], 10
    if flags & 4:
        pos += 2 + int.from_bytes(data[pos:pos + 2], "little")
    if flags & 8:
        pos = data.index(b"\0", pos) + 1
    if flags & 16:
        pos = data.index(b"\0", pos) + 1
    if flags & 2:
        pos += 2
    d = zlib.decompressobj(-15)
    n = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb") as out:
        for i in range(pos, len(data), CHUNK):
            chunk = d.decompress(data[i:i + CHUNK])
            n += len(chunk)
            budget.add_bytes(len(chunk), n)
            out.write(chunk)
            if d.eof:
                break
    if not d.eof:
        raise ValueError("deflate stream truncated; unrecoverable")


def extract_unix_compress(path: Path, dest: Path, budget: Budget, notes: list, name: str):
    out_name = _single_stream_name(name, (".z",))
    budget.add_member()
    # .Z files are small (max 87 MB here); decompress in memory, then apply caps.
    data = ncompress.decompress(path.read_bytes())
    budget.add_bytes(len(data), len(data))
    p = dest / safe_relpath(out_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "xb") as f:
        f.write(data)


def extract_7z(path: Path, dest: Path, budget: Budget, notes: list):
    """7z/rar via the 7z CLI: list + validate first, then extract, then re-verify on disk."""
    listing = subprocess.run(["7z", "l", "-slt", "-ba", "-p", str(path)], capture_output=True, text=True,
                             errors="replace", timeout=600)
    if listing.returncode != 0:
        if "encrypted archive" in (listing.stdout + listing.stderr):
            raise Quarantine("encrypted 7z/rar archive (encrypted headers)")
        raise Quarantine(f"7z list failed: {listing.stderr.strip()[:200]}")
    entries, cur = [], {}
    for line in listing.stdout.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        if " = " in line:
            k, v = line.split(" = ", 1)
            cur[k.strip()] = v
    if cur:
        entries.append(cur)
    declared = 0
    for e in entries:
        attrs = e.get("Attributes", "")
        if attrs.startswith("D") or e.get("Folder") == "+":
            continue
        unix_mode = attrs.split(" ")[-1] if " " in attrs else ""  # e.g. "A_ -rw-r--r--" / "_ lrwxrwxrwx"
        if unix_mode.startswith("l") or e.get("Symbolic Link"):
            raise Quarantine(f"symlink entry {e.get('Path')!r}")
        if e.get("Encrypted") == "+":
            raise Quarantine("encrypted 7z/rar member")
        safe_relpath(e.get("Path", ""))
        budget.add_member()
        declared += int(e.get("Size") or 0)
    if declared > MAX_TOTAL_BYTES:
        raise Quarantine(f"declared size {declared} exceeds cap")
    if declared > 64 * 1024**2 and declared / budget.compressed > MAX_RATIO:
        raise Quarantine("declared compression ratio too high")
    res = subprocess.run(["7z", "x", "-y", "-p", "-snl-", f"-o{dest}", str(path)], capture_output=True, text=True,
                         errors="replace", timeout=3600)
    if res.returncode != 0:
        raise Quarantine(f"7z extract failed: {res.stderr.strip()[:200]}")
    # Re-verify what actually landed on disk.
    root = dest.resolve()
    for p in dest.rglob("*"):
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            raise Quarantine(f"symlink materialised: {p.relative_to(dest)}")
        if not p.resolve().is_relative_to(root):
            raise Quarantine(f"path escaped extraction root: {p}")
        if stat.S_ISREG(st.st_mode):
            budget.add_bytes(st.st_size, st.st_size)


def extract(fmt: str, path: Path, dest: Path, budget: Budget, notes: list, name: str):
    if fmt in ("zip", "zip-other"):
        extract_zip(path, dest, budget, notes)
    elif fmt == "tar":
        extract_tar(path, dest, budget, notes)
    elif fmt == "gzip":
        extract_gzip(path, dest, budget, notes, name)
    elif fmt == "unix-compress":
        extract_unix_compress(path, dest, budget, notes, name)
    elif fmt in ("7z", "rar"):
        extract_7z(path, dest, budget, notes)
    else:
        raise Quarantine(f"unsupported archive format {fmt}")


# ---------------------------------------------------------------- driver
def sha256_and_head(p: Path) -> tuple[str, bytes]:
    h = hashlib.sha256()
    head = b""
    with open(p, "rb") as f:
        while chunk := f.read(CHUNK):
            if len(head) < HEAD_BYTES:
                head += chunk[: HEAD_BYTES - len(head)]
            h.update(chunk)
    return h.hexdigest(), head


def unpack_blob(job: dict) -> dict:
    """Unpack one archive blob (recursively). Returns {'archives': [...], 'members': [...]}."""
    s3 = s3_client()
    archives, members = [], []
    work = Path(tempfile.mkdtemp(prefix="unpack-", dir=job["tmp"]))
    try:
        src = work / "archive.bin"
        s3.download_file(STAGING_BUCKET, job["key"], str(src))
        queue = [(job["sha256"], job["format"], src, job["name"], 0, None)]
        seen = set()
        while queue:
            sha, fmt, path, name, depth, parent = queue.pop()
            if sha in seen:
                continue
            seen.add(sha)
            rec = {"archive_sha256": sha, "format": fmt, "name": name, "depth": depth, "parent_sha256": parent,
                   "compressed_bytes": path.stat().st_size, "status": "ok", "reason": None,
                   "n_members": 0, "uncompressed_bytes": 0, "ratio_flag": False, "notes": []}
            archives.append(rec)
            if depth > MAX_DEPTH:
                rec["status"], rec["reason"] = "left-packed", f"nesting depth > {MAX_DEPTH}"
                continue
            dest = work / f"x-{sha[:16]}"
            dest.mkdir()
            budget = Budget(rec["compressed_bytes"])
            t0 = time.time()
            try:
                extract(fmt, path, dest, budget, rec["notes"], name)
            except Quarantine as q:
                rec["status"], rec["reason"] = "quarantined", str(q)
                shutil.rmtree(dest, ignore_errors=True)
                continue
            except Exception as e:  # corrupt / truncated archive etc.
                rec["status"], rec["reason"] = "error", f"{type(e).__name__}: {e}"[:300]
                shutil.rmtree(dest, ignore_errors=True)
                continue
            if any(n.startswith("recovered:") for n in rec["notes"]):
                rec["status"] = "recovered"
            rec["seconds"] = round(time.time() - t0, 2)
            rec["uncompressed_bytes"] = budget.total
            rec["ratio_flag"] = budget.total / max(rec["compressed_bytes"], 1) > FLAG_RATIO
            files = [p for p in sorted(dest.rglob("*")) if p.is_file() and not p.is_symlink()]

            def _member(p: Path) -> dict:
                rel = p.relative_to(dest).as_posix()
                msha, head = sha256_and_head(p)
                mfmt = detect(rel, head) if p.stat().st_size else "empty"
                s3_key = f"{UNPACK_PREFIX}/{sha}/{rel}"
                s3.upload_file(str(p), STAGING_BUCKET, s3_key)
                return {"archive_sha256": sha, "member_path": rel, "size": p.stat().st_size,
                        "sha256": msha, "format": mfmt, "depth": depth + 1, "s3_key": s3_key,
                        "head16": head[:16].hex(), "is_archive": mfmt in ARCHIVE_FORMATS, "_path": p}

            # Archives can hold tens of thousands of small members: upload in parallel.
            with ThreadPoolExecutor(16) as up:
                results = list(up.map(_member, files))
            for mrow in results:
                p = mrow.pop("_path")
                rec["n_members"] += 1
                members.append(mrow)
                if mrow["is_archive"]:
                    queue.append((mrow["sha256"], mrow["format"], p, p.name, depth + 1, sha))
        return {"archives": archives, "members": members}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--include-paired", action="store_true",
                    help="also unpack archives that already have an .extracted/ sibling")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-run only depth-0 archives that errored / were quarantined (not encrypted) "
                         "and merge into the existing archives/members parquet")
    args = ap.parse_args(argv)
    prev_archives = prev_members = None
    retry = None
    if args.retry_failed:
        prev_archives = pq.read_table(MANIFEST_DIR / "archives.parquet").to_pylist()
        prev_members = pq.read_table(MANIFEST_DIR / "members.parquet").to_pylist()
        retry = {a["archive_sha256"] for a in prev_archives
                 if a["depth"] == 0 and a["status"] in ("error", "quarantined")
                 and "encrypted" not in (a["reason"] or "").lower() and "7z list failed" not in (a["reason"] or "")}
        print(f"retrying {len(retry)} failed archives", flush=True)

    m = pq.read_table(MANIFEST_DIR / "manifest.parquet",
                      columns=["key", "sha256", "format", "is_archive", "archive_paired", "is_canonical",
                               "stage_status", "size"]).to_pylist()
    jobs, seen = [], set()
    for r in m:
        if not r["is_archive"] or r["stage_status"] != "ok" or r["sha256"] in seen:
            continue
        if r["archive_paired"] and not args.include_paired:
            continue
        if retry is not None and r["sha256"] not in retry:
            continue
        seen.add(r["sha256"])
        jobs.append({"key": r["key"], "sha256": r["sha256"], "format": r["format"],
                     "name": r["key"].rsplit("/", 1)[-1], "size": r["size"]})
    jobs.sort(key=lambda j: -j["size"])
    if args.limit:
        jobs = jobs[: args.limit]
    tmp = Path(os.environ.get("DEID_TMP", Path.home() / "data" / "machina" / "tmp"))
    tmp.mkdir(parents=True, exist_ok=True)
    for j in jobs:
        j["tmp"] = str(tmp)
    print(f"unpacking {len(jobs):,} unique archive blobs ({sum(j['size'] for j in jobs)/1e9:.2f} GB)", flush=True)

    archives, members = [], []
    t0 = time.time()
    with ProcessPoolExecutor(args.workers) as pool:
        futs = {pool.submit(unpack_blob, j): j for j in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            j = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # download failure etc.
                res = {"archives": [{"archive_sha256": j["sha256"], "format": j["format"], "name": j["name"],
                                     "depth": 0, "status": "error", "reason": f"{type(e).__name__}: {e}"[:300]}],
                       "members": []}
            archives += res["archives"]
            members += res["members"]
            if i % 200 == 0 or i == len(futs):
                print(f"[{(time.time()-t0)/60:5.1f} min] {i:,}/{len(futs):,} archives | {len(members):,} members",
                      flush=True)

    for a in archives:
        a["notes"] = json.dumps(a.get("notes", []), ensure_ascii=False)
    if retry is not None:  # merge: replace the retried archives' rows, keep everything else
        redone = {a["archive_sha256"] for a in archives}
        archives = [a for a in prev_archives if a["archive_sha256"] not in redone] + archives
        members = [m for m in prev_members if m["archive_sha256"] not in redone] + members
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(archives), MANIFEST_DIR / "archives.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(members), MANIFEST_DIR / "members.parquet", compression="zstd")
    s3 = s3_client()
    for name in ("archives.parquet", "members.parquet"):
        s3.upload_file(str(MANIFEST_DIR / name), STAGING_BUCKET, f"_manifest/{name}")

    from collections import Counter
    print(json.dumps({
        "archive_blobs_total": len(archives),
        "by_status": Counter(a["status"] for a in archives),
        "by_depth": Counter(a["depth"] for a in archives),
        "members": len(members),
        "member_bytes": sum(x["size"] for x in members),
        "unique_member_blobs": len({x["sha256"] for x in members}),
        "ratio_flagged": sum(bool(a.get("ratio_flag")) for a in archives),
        "member_formats": Counter(x["format"] for x in members).most_common(25),
        "problem_reasons": Counter((a["reason"] or "")[:80] for a in archives if a["status"] != "ok").most_common(15),
    }, indent=2, default=dict, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
