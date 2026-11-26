"""Stage 0b: build manifest.parquet (one row per object) and blobs.parquet
(one row per unique sha256) from the staging results.

Inputs : <MANIFEST_DIR>/stage_results.jsonl  (written by deid.stage)
         a fresh R2 listing (so skipped / failed / missing keys are accounted for)
Outputs: <MANIFEST_DIR>/manifest.parquet, blobs.parquet, manifest_summary.json
         and the same files uploaded to s3://<STAGING_BUCKET>/_manifest/

Usage:  python -m deid.manifest [--no-upload]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict

import pyarrow as pa
import pyarrow.parquet as pq

from deid.config import MANIFEST_DIR, R2_PREFIX, STAGING_BUCKET, s3_client
from deid.formats import detect, split_ext
from deid.stage import RESULTS, list_r2

ARCHIVE_FORMATS = {"zip", "zip-other", "7z", "rar", "gzip", "unix-compress", "tar"}
EXTRACTED = ".extracted"


def load_stage_results() -> dict[str, dict]:
    """Last ok record wins per key; errors kept only if no ok record exists."""
    best: dict[str, dict] = {}
    with RESULTS.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:  # partially written last line
                continue
            prev = best.get(r["key"])
            if prev is None or r["status"] == "ok" or prev["status"] != "ok":
                best[r["key"]] = r
    return best


def path_info(key: str) -> dict:
    parts = key.split("/")
    top = parts[1] if len(parts) > 1 else ""
    owner = repo = commit = None
    if top in ("github", "gitlab") and len(parts) > 2:
        seg = parts[2].split("__")
        owner = seg[0]
        repo = seg[1] if len(seg) > 1 else None
        commit = seg[2] if len(seg) > 2 else None
    # Archive that this key was extracted from: nearest ancestor dir ending in .extracted
    extracted_dir = None
    for i in range(len(parts) - 2, 0, -1):
        if parts[i].endswith(EXTRACTED):
            extracted_dir = "/".join(parts[: i + 1])
            break
    return {"top_prefix": top, "owner": owner, "repo": repo, "commit": commit,
            "depth": len(parts) - 1, "extracted_dir": extracted_dir}


def extracted_candidates(archive_key: str) -> list[str]:
    """`a/b.tar.gz` -> [`a/b.tar.gz.extracted`, `a/b.tar.extracted`, `a/b.extracted`]."""
    out = [archive_key + EXTRACTED]
    dirname, _, name = archive_key.rpartition("/")
    stem = name
    for _ in range(2):
        if "." not in stem:
            break
        stem = stem.rsplit(".", 1)[0]
        out.append(f"{dirname}/{stem}{EXTRACTED}" if dirname else stem + EXTRACTED)
    return out


def build(upload: bool = True) -> dict:
    staged = load_stage_results()
    listing = list(list_r2(R2_PREFIX))
    rows = []
    for o in listing:
        k = o["key"]
        st = staged.get(k, {})
        head = bytes.fromhex(st.get("head_hex", "")) if st.get("status") == "ok" else b""
        ext, base = split_ext(k)
        if k.endswith(".resume-partial"):
            fmt, status, action, reason = "partial-download", "skipped", "skip", "truncated download (.resume-partial)"
        elif st.get("status") != "ok":
            fmt, status, action, reason = "unknown", st.get("status", "missing"), "retry", st.get("error", "not staged")
        else:
            fmt = detect(k, head) if o["size"] else "empty"
            status = "ok"
            action, reason = ("copy", "zero-byte") if o["size"] == 0 else ("process", None)
        rows.append({
            "key": k, "size": o["size"], "r2_etag": o["r2_etag"], "sha256": st.get("sha256"),
            "head16": head[:16].hex(), "ext": ext, "base_ext": base, "format": fmt,
            "stage_status": status, "action": action, "action_reason": reason,
            **path_info(k),
        })

    # --- archive <-> .extracted pairing
    extracted_dirs = {r["extracted_dir"] for r in rows if r["extracted_dir"]}
    by_key = {r["key"]: r for r in rows}
    claimed_dirs = set()
    for r in rows:
        r["is_archive"] = r["format"] in ARCHIVE_FORMATS
        r["extracted_sibling"] = None
        if r["is_archive"]:
            for cand in extracted_candidates(r["key"]):
                if cand in extracted_dirs:
                    r["extracted_sibling"] = cand
                    claimed_dirs.add(cand)
                    break
        r["archive_paired"] = r["extracted_sibling"] is not None
    dir_to_archive = {r["extracted_sibling"]: r["key"] for r in rows if r["extracted_sibling"]}
    for r in rows:
        r["from_archive"] = dir_to_archive.get(r["extracted_dir"]) if r["extracted_dir"] else None

    # --- dedup by sha256
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["sha256"]:
            groups[r["sha256"]].append(r)
    blobs = []
    for sha, members in groups.items():
        members.sort(key=lambda r: r["key"])
        canon = members[0]["key"]
        for r in members:
            r["dup_count"] = len(members)
            r["canonical_key"] = canon
            r["is_canonical"] = r["key"] == canon
        blobs.append({"sha256": sha, "size": members[0]["size"], "format": members[0]["format"],
                      "n_keys": len(members), "canonical_key": canon, "keys": [m["key"] for m in members],
                      "n_prefixes": len({m["top_prefix"] for m in members}),
                      "n_owners": len({m["owner"] for m in members if m["owner"]})})
    for r in rows:
        r.setdefault("dup_count", 0)
        r.setdefault("canonical_key", None)
        r.setdefault("is_canonical", False)

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), MANIFEST_DIR / "manifest.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(blobs), MANIFEST_DIR / "blobs.parquet", compression="zstd")

    # --- summary
    ok = [r for r in rows if r["stage_status"] == "ok"]
    unique_bytes = sum(b["size"] for b in blobs)
    total_bytes = sum(r["size"] for r in ok)
    fmt_count, fmt_bytes = Counter(), Counter()
    for b in blobs:
        fmt_count[b["format"]] += 1
        fmt_bytes[b["format"]] += b["size"]
    archives = [r for r in rows if r["is_archive"]]
    summary = {
        "objects_listed": len(rows),
        "objects_staged_ok": len(ok),
        "objects_not_ok": Counter(r["stage_status"] for r in rows if r["stage_status"] != "ok"),
        "zero_byte": sum(r["size"] == 0 for r in rows),
        "bytes_staged": total_bytes,
        "unique_blobs": len(blobs),
        "unique_bytes": unique_bytes,
        "dedup_saving_objects_pct": round(100 * (1 - len(blobs) / max(len(ok), 1)), 2),
        "dedup_saving_bytes_pct": round(100 * (1 - unique_bytes / max(total_bytes, 1)), 2),
        "blobs_spanning_multiple_owners": sum(b["n_owners"] > 1 for b in blobs),
        "archives": {
            "total": len(archives),
            "paired_with_extracted": sum(r["archive_paired"] for r in archives),
            "unpaired": sum(not r["archive_paired"] for r in archives),
            "unpaired_bytes": sum(r["size"] for r in archives if not r["archive_paired"]),
            "by_format": Counter(r["format"] for r in archives),
            "extracted_dirs_without_archive": len(extracted_dirs - claimed_dirs),
        },
        "unique_blobs_by_format": dict(fmt_count.most_common()),
        "unique_gb_by_format": {k: round(v / 1e9, 3) for k, v in fmt_bytes.most_common()},
        "ext_vs_format_mismatches": Counter(
            f"{r['base_ext']}->{r['format']}" for r in ok
            if r["format"].startswith(("zip", "gzip", "pdf", "unknown", "7z"))
            and r["base_ext"] not in ("zip", "gz", "pdf", "7z", "fcstd", "3mf", "f3d", "f3z", "odg", "tgz")
        ).most_common(25),
    }
    (MANIFEST_DIR / "manifest_summary.json").write_text(json.dumps(summary, indent=2, default=dict))

    if upload:
        s3 = s3_client()
        for name in ("manifest.parquet", "blobs.parquet", "manifest_summary.json"):
            s3.upload_file(str(MANIFEST_DIR / name), STAGING_BUCKET, f"_manifest/{name}")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args(argv)
    print(json.dumps(build(upload=not args.no_upload), indent=2, default=dict))


if __name__ == "__main__":
    main()
