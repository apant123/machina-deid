"""One-off corpus audit of the R2 source bucket (writes r2_audit.json).

Run from the repo root: .venv312/bin/python -m tools.inspect_r2
Credentials come from the environment / .env via deid.config.
"""
import json
import os
from collections import Counter, defaultdict

from deid.config import R2_BUCKET, R2_PREFIX, ROOT, r2_client

s3 = r2_client()

BUCKET = R2_BUCKET
PREFIX = R2_PREFIX
ARCHIVE_EXTS = {"zip", "7z", "rar", "tar", "gz", "tgz", "z"}

# Counters & accumulators
total_objs = 0
total_bytes = 0
zero_byte = 0
non_ascii_keys = 0
max_depth = 0

ext_counts = Counter()
ext_bytes = Counter()
ext_max_size = Counter()
compound_numeric_exts = Counter()  # e.g., prt.1, asm.2

top_prefix_counts = Counter()
top_prefix_bytes = Counter()
repo_owners = defaultdict(set)  # github/gitlab owners

etag_seen = {}  # (etag, size) -> count
largest_files = []  # (size, key)

archive_keys = []
extracted_prefixes = set()

# Sample keys for 4KB magic-byte probing
probe_targets = {"sldprt": [], "sldasm": [], "slddrw": [], "ipt": [], "iam": [], "stl": [], "1": []}
PROBE_LIMIT = 40

paginator = s3.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX):
    for obj in page.get("Contents", []):
        key = obj["Key"]
        size = obj["Size"]
        etag = obj["ETag"].strip('"')

        total_objs += 1
        total_bytes += size
        if size == 0:
            zero_byte += 1
        if not key.isascii():
            non_ascii_keys += 1

        parts = key.split("/")
        max_depth = max(max_depth, len(parts))
        top = parts[1] if len(parts) > 1 else "<root>"
        top_prefix_counts[top] += 1
        top_prefix_bytes[top] += size

        if top in ("github", "gitlab") and len(parts) > 2:
            repo_owners[top].add(parts[2])

        # Track .extracted/ directories
        for i, p in enumerate(parts[:-1]):
            if p.endswith(".extracted"):
                extracted_prefixes.add("/".join(parts[: i + 1]))

        fname = parts[-1]
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "<none>"
        ext_counts[ext] += 1
        ext_bytes[ext] += size
        if size > ext_max_size[ext]:
            ext_max_size[ext] = size

        # Compound numeric extensions (Creo .prt.1, .asm.2, etc.)
        if ext.isdigit() and fname.count(".") >= 2:
            prev_ext = fname.rsplit(".", 2)[-2].lower()
            compound_numeric_exts[f"{prev_ext}.<num>"] += 1

        if ext in ARCHIVE_EXTS:
            archive_keys.append((key, size))

        # Dedup tracking
        sig = (etag, size)
        etag_seen[sig] = etag_seen.get(sig, 0) + 1

        # Top 20 largest files
        if len(largest_files) < 20 or size > largest_files[-1][0]:
            largest_files.append((size, key))
            largest_files.sort(reverse=True)
            largest_files = largest_files[:20]

        # Collect probe samples
        if ext in probe_targets and size > 128 and len(probe_targets[ext]) < PROBE_LIMIT:
            probe_targets[ext].append(key)

# Dedup math
unique_objs = len(etag_seen)
unique_bytes = sum(size for (_, size) in etag_seen.keys())

# Archive vs .extracted/ pairing
paired_archives = 0
unpaired_archives = 0
unpaired_archive_bytes = 0
for akey, asize in archive_keys:
    stem = akey.rsplit(".", 1)[0]
    if f"{stem}.extracted" in extracted_prefixes or f"{akey}.extracted" in extracted_prefixes:
        paired_archives += 1
    else:
        unpaired_archives += 1
        unpaired_archive_bytes += asize

# 4KB Range-read magic-byte probe
def classify_header(ext, data):
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "OLE2 (D0CF11E0)"
    if data.startswith(b"PK\x03\x04"):
        return "ZIP-container (PK)"
    if ext == "stl":
        # ASCII STL starts with 'solid ' and contains 'facet' or 'endsolid'
        head_txt = data[:512].decode("ascii", errors="ignore").lower()
        return "ASCII STL" if (head_txt.startswith("solid") and ("facet" in head_txt or "vertex" in head_txt)) else "Binary STL"
    if data.startswith(b"#UGC:"):
        return "Creo (#UGC)"
    return f"Other ({data[:8].hex()})"

probe_results = {}
for ext, keys in probe_targets.items():
    c = Counter()
    for k in keys:
        try:
            resp = s3.get_object(Bucket=BUCKET, Key=k, Range="bytes=0-4095")
            c[classify_header(ext, resp["Body"].read())] += 1
        except Exception as e:
            c[f"Error: {type(e).__name__}"] += 1
    probe_results[ext] = dict(c)

# Print concise summary
GB = 1024 ** 3
MB = 1024 ** 2
print(f"=== CORPUS OVERVIEW ===")
print(f"Objects: {total_objs:,} | Total: {total_bytes/GB:.2f} GB | Zero-byte: {zero_byte} | Non-ASCII keys: {non_ascii_keys} | Max depth: {max_depth}")
print(f"Dedup (ETag+Size): {unique_objs:,} unique ({unique_objs/total_objs*100:.1f}%) | {unique_bytes/GB:.2f} GB unique ({(1-unique_bytes/total_bytes)*100:.1f}% savings)")

print(f"\n=== TOP-LEVEL PREFIXES ===")
for p, cnt in top_prefix_counts.most_common():
    extra = f" | {len(repo_owners[p])} unique owners" if p in repo_owners else ""
    print(f"  {p:12s}: {cnt:7,d} objs | {top_prefix_bytes[p]/GB:6.2f} GB{extra}")

print(f"\n=== TOP 15 EXTENSIONS BY BYTES (with max file size) ===")
for ext, b in ext_bytes.most_common(15):
    print(f"  .{ext:10s}: {ext_counts[ext]:6,d} objs | {b/GB:6.2f} GB | max {ext_max_size[ext]/MB:7.1f} MB")

print(f"\n=== COMPOUND NUMERIC EXTENSIONS (e.g. Creo) ===")
for k, v in compound_numeric_exts.most_common(10):
    print(f"  {k:15s}: {v:,}")

print(f"\n=== ARCHIVES vs .extracted/ ===")
print(f"  Paired with .extracted/: {paired_archives:,} | Unpaired (must unpack): {unpaired_archives:,} ({unpaired_archive_bytes/GB:.2f} GB)")

print(f"\n=== 4KB MAGIC-BYTE PROBE (sample of {PROBE_LIMIT}/ext) ===")
for ext, res in probe_results.items():
    print(f"  .{ext:8s}: {res}")

print(f"\n=== TOP 10 LARGEST FILES ===")
for s, k in largest_files[:10]:
    print(f"  {s/MB:8.1f} MB  {k}")

# Save full report for documentation
report = {
    "total_objs": total_objs,
    "total_bytes": total_bytes,
    "unique_objs": unique_objs,
    "unique_bytes": unique_bytes,
    "zero_byte": zero_byte,
    "non_ascii_keys": non_ascii_keys,
    "top_prefixes": {k: {"count": top_prefix_counts[k], "bytes": top_prefix_bytes[k]} for k in top_prefix_counts},
    "repo_owner_counts": {k: len(v) for k, v in repo_owners.items()},
    "ext_stats": {k: {"count": ext_counts[k], "bytes": ext_bytes[k], "max_bytes": ext_max_size[k]} for k in ext_counts},
    "compound_numeric_exts": dict(compound_numeric_exts),
    "archives": {"paired": paired_archives, "unpaired": unpaired_archives, "unpaired_bytes": unpaired_archive_bytes},
    "magic_probe": probe_results,
    "largest_files": [{"bytes": s, "key": k} for s, k in largest_files],
}
with open(ROOT / "r2_audit.json", "w") as f:
    json.dump(report, f, indent=2)
print("\nSaved full report to ~/machina-deid/r2_audit.json")