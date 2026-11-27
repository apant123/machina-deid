"""Stage 3a: logo detection pass (OWLv2, CPU) over every unique raster image, cached by image sha256.

    python -m deid.logo_scan extract --workers 15   # PDFs + images -> unique image files (private dir)
    python -m deid.logo_scan detect  --workers 15   # OWLv2 on each unique image -> logo_scan.parquet
    python -m deid.logo_scan sheet                  # contact sheets for review (private dir; real logos)

Images are hashed exactly as the Stage 3 writers see them (PDF: Pixmap(xref) -> PNG bytes; standalone:
file bytes), so the rewrite looks detections up by hash instead of running the detector again.
The scan keeps every box scoring >= SCAN_THRESHOLD (below the production threshold) so the review
can choose the cut-off.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from multiprocessing import get_context
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .config import DATA_DIR, STAGING_BUCKET, s3_client
from .harvest_run import HARVEST_DIR

LOGO_DIR = DATA_DIR / "logos"
IMG_DIR = LOGO_DIR / "images"
SCAN_THRESHOLD = 0.10
IMAGE_FORMATS = {"png", "jpeg", "tiff", "unknown:gif", "unknown:jpg", "unknown:bmp", "unknown:jpeg", "unknown:tif"}
_s3 = None


def pdf_images(path):
    """Yield (xref, png_bytes) exactly as deid.rewrite.pdf._images extracts them."""
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    with pymupdf.open(path) as doc:
        seen = set()
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if pix.width < 48 or pix.height < 24:
                        continue
                    if pix.n - pix.alpha >= 4:
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    yield xref, pix.tobytes("png")
                except Exception:  # noqa: BLE001
                    continue


def _store(data: bytes) -> str:
    h = hashlib.sha256(data).hexdigest()
    p = IMG_DIR / h[:2] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp%d" % os.getpid())
        tmp.write_bytes(data)
        os.replace(tmp, p)
    return h


def _extract_one(job):
    global _s3
    if _s3 is None:
        _s3 = s3_client()
    fd, tmp = tempfile.mkstemp(dir=LOGO_DIR / "tmp")
    os.close(fd)
    out = []
    try:
        _s3.download_file(STAGING_BUCKET, job["s3_key"], tmp)
        if job["format"] == "pdf":
            for xref, png in pdf_images(tmp):
                out.append((job["sha256"], xref, _store(png)))
        else:
            out.append((job["sha256"], -1, _store(Path(tmp).read_bytes())))
    except Exception as e:  # noqa: BLE001
        out.append((job["sha256"], -2, f"error: {e!r}"[:200]))
    finally:
        os.unlink(tmp)
    return out


def extract(workers: int):
    (LOGO_DIR / "tmp").mkdir(parents=True, exist_ok=True)
    jobs = [r for r in pq.read_table(HARVEST_DIR / "jobs.parquet").to_pylist() if r["format"] in IMAGE_FORMATS | {"pdf"}]
    rows, t0 = [], time.time()
    with get_context("fork").Pool(workers) as pool:
        for i, res in enumerate(pool.imap_unordered(_extract_one, jobs, chunksize=4), 1):
            rows += res
            if i % 500 == 0:
                print(f"{i}/{len(jobs)} files, {len(rows)} images, {time.time() - t0:.0f}s", flush=True)
    pq.write_table(pa.table({"src_sha": [r[0] for r in rows], "xref": [r[1] for r in rows], "img_sha": [r[2] for r in rows]}),
                   LOGO_DIR / "images.parquet")
    print(dict(files=len(jobs), images=len(rows), unique=len({r[2] for r in rows if r[1] >= -1})))


_model = None


def _detect_one(h):
    global _model
    import warnings
    warnings.filterwarnings("ignore")
    from PIL import Image
    from .rewrite import image as I
    Image.MAX_IMAGE_PIXELS = 600_000_000
    t = time.time()
    try:
        im = Image.open(IMG_DIR / h[:2] / h)
        im.load()
        if min(im.width, im.height) < 64:  # icons / rules: too small to carry a logo worth detecting
            return h, im.width, im.height, "[]", 0.0
        sm = im.convert("L").resize((32, 32))
        lo, hi = sm.getextrema()
        if hi - lo < 8:  # blank / flat fill
            return h, im.width, im.height, "[]", 0.0
        work = im
        if im.width * im.height > I.MAX_PIXELS:
            f = (I.MAX_PIXELS / (im.width * im.height)) ** 0.5
            work = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))))
        old = I.LOGO_THRESHOLD
        I.LOGO_THRESHOLD = SCAN_THRESHOLD
        try:
            boxes = I.detect_logos(work)
        finally:
            I.LOGO_THRESHOLD = old
        return h, im.width, im.height, json.dumps([[list(b), round(s, 4)] for b, s in boxes]), time.time() - t
    except Exception as e:  # noqa: BLE001
        return h, 0, 0, json.dumps({"error": repr(e)[:200]}), time.time() - t


def detect(workers: int):
    os.environ.setdefault("DEID_TORCH_THREADS", "1")
    imgs = pq.read_table(LOGO_DIR / "images.parquet").to_pylist()
    uniq = sorted({r["img_sha"] for r in imgs if r["xref"] >= -1})
    done = set()
    parts = sorted(LOGO_DIR.glob("scan-*.parquet"))
    for p in parts:
        done.update(pq.read_table(p, columns=["img_sha"]).column(0).to_pylist())
    todo = [h for h in uniq if h not in done]
    print(f"unique images {len(uniq)}, todo {len(todo)}", flush=True)
    rows, t0, n = [], time.time(), 0

    def flush():
        if rows:
            pq.write_table(pa.table({k: [r[i] for r in rows] for i, k in enumerate(["img_sha", "w", "h", "boxes", "sec"])}),
                           LOGO_DIR / f"scan-{len(list(LOGO_DIR.glob('scan-*.parquet'))):05d}.parquet")
            rows.clear()
    with get_context("fork").Pool(workers) as pool:
        for r in pool.imap_unordered(_detect_one, todo, chunksize=2):
            rows.append(r)
            n += 1
            if len(rows) >= 500:
                flush()
                el = time.time() - t0
                print(f"{n}/{len(todo)} {el / 60:.1f} min, eta {(len(todo) - n) * el / n / 60:.0f} min", flush=True)
    flush()
    t = pa.concat_tables([pq.read_table(p) for p in sorted(LOGO_DIR.glob("scan-*.parquet"))])
    pq.write_table(t, LOGO_DIR / "logo_scan.parquet")
    print("detect finished", t.num_rows, flush=True)


def load_cache(threshold: float = SCAN_THRESHOLD) -> dict:
    """img_sha -> [(box, score)] for the rewrite (the writer applies the production threshold)."""
    p = LOGO_DIR / "logo_scan.parquet"
    if not p.exists():
        return None
    out = {}
    for r in pq.read_table(p).to_pylist():
        b = json.loads(r["boxes"])
        if isinstance(b, list):
            out[r["img_sha"]] = [(tuple(x[0]), x[1]) for x in b if x[1] >= threshold]
    return out


def sheet(per_band: int = 120):
    """Contact sheets: one PNG per score band, crops with their score (and 8 px context)."""
    from PIL import Image, ImageDraw, ImageFont
    from .rewrite import image as I
    Image.MAX_IMAGE_PIXELS = 600_000_000
    rows = pq.read_table(LOGO_DIR / "logo_scan.parquet").to_pylist()
    dets = []
    for r in rows:
        b = json.loads(r["boxes"])
        if isinstance(b, list):
            dets += [(s, r["img_sha"], box, r["w"], r["h"]) for box, s in b]
    bands = [(0.30, 1.01), (0.22, 0.30), (0.18, 0.22), (0.14, 0.18), (0.10, 0.14)]
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    out_dir = LOGO_DIR / "sheets"
    out_dir.mkdir(exist_ok=True)
    summary = {}
    import random
    random.seed(3)
    for lo, hi in bands:
        sel = [d for d in dets if lo <= d[0] < hi]
        summary[f"{lo:.2f}-{hi:.2f}"] = dict(detections=len(sel), images=len({d[1] for d in sel}))
        pick = sorted(random.sample(sel, min(per_band, len(sel))), key=lambda d: -d[0])
        cell, cols = 160, 10
        sheet_im = Image.new("RGB", (cols * cell, max(1, (len(pick) + cols - 1) // cols) * (cell + 16)), "white")
        dr = ImageDraw.Draw(sheet_im)
        for k, (s, h, box, w, hh) in enumerate(pick):
            im = Image.open(IMG_DIR / h[:2] / h).convert("RGB")
            if im.width * im.height > I.MAX_PIXELS:  # boxes are in downscaled coordinates
                f = (I.MAX_PIXELS / (im.width * im.height)) ** 0.5
                im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))))
            x0, y0, x1, y1 = box
            crop = im.crop((max(0, x0 - 8), max(0, y0 - 8), min(im.width, x1 + 8), min(im.height, y1 + 8)))
            crop.thumbnail((cell - 4, cell - 4))
            cx, cy = (k % cols) * cell, (k // cols) * (cell + 16)
            sheet_im.paste(crop, (cx + 2, cy + 2))
            dr.text((cx + 2, cy + cell), f"{s:.2f}  {w}x{hh}", fill="black", font=font)
        sheet_im.save(out_dir / f"logos_{lo:.2f}-{hi:.2f}.png")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["extract", "detect", "sheet"])
    ap.add_argument("--workers", type=int, default=15)
    a = ap.parse_args()
    {"extract": lambda: extract(a.workers), "detect": lambda: detect(a.workers), "sheet": sheet}[a.cmd]()
    sys.exit(0)
