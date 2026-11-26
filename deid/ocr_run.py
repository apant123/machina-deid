"""Stage 1b: OCR text harvest from rasters (entity discovery only).

Targets
* standalone images (png / jpeg / tiff / gif / bmp, incl. archive members)
* PDF pages without a usable text layer (scanned drawings) -> rendered at 150 dpi
* raster images embedded in PDFs (logos, pasted title blocks), >= 48x24 px

Not targeted: CAD preview thumbnails (SolidWorks / OLE / FreeCAD / 3MF). They are tiny
renders of the model; Stage 3 replaces them with neutral images in length-sensitive
formats and runs the image scrubber on re-encodable ones, so OCR here adds cost but
no entities. Logo *detection* (non-text logos) also belongs to the Stage 3 scrubber.

    python -m deid.ocr_run run [--workers 8]
    python -m deid.ocr_run aggregate
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tempfile
import time
import traceback
from collections import Counter
from multiprocessing import get_context

import pyarrow as pa
import pyarrow.parquet as pq

from .config import STAGING_BUCKET, s3_client
from .harvest import norm
from .harvest_run import HARVEST_DIR

OCR_DIR = HARVEST_DIR / "ocr"
IMAGE_FORMATS = {"png", "jpeg", "tiff", "unknown:gif", "unknown:jpg", "unknown:bmp", "unknown:jpeg", "unknown:tif"}
MIN_SCORE = 0.6
MAX_PDF_IMAGES = 40
MAX_PDF_RENDER_PAGES = 25
MAX_PIXELS = 40_000_000

SCHEMA = pa.schema([("sha256", pa.string()), ("kind", pa.string()), ("value", pa.string()), ("count", pa.int64()),
                    ("score", pa.float64())])
STATUS = pa.schema([("sha256", pa.string()), ("format", pa.string()), ("status", pa.string()), ("error", pa.string()),
                    ("images_ocrd", pa.int64()), ("pages_rendered", pa.int64()), ("seconds", pa.float64())])

_eng = None
_s3 = None


def _init():
    global _eng, _s3
    import logging
    import cv2
    cv2.setNumThreads(1)  # OpenCV's own pool otherwise oversubscribes the box (load 43 on 16 vCPU)
    from rapidocr import RapidOCR
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    _eng = RapidOCR(params={"Global.log_level": "error", "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                            "EngineConfig.onnxruntime.inter_op_num_threads": 1})
    _s3 = s3_client()


class NotAnImage(Exception):
    pass


def _ocr(img_bytes: bytes, strict: bool = False) -> list[tuple[str, float]]:
    from PIL import Image, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = 600_000_000  # large scanned drawings are legitimate; we downscale below
    try:
        im = Image.open(io.BytesIO(img_bytes))
        im.load()
    except (UnidentifiedImageError, OSError) as e:
        if strict:
            raise NotAnImage(repr(e)) from e
        return []  # one broken image inside a PDF must not fail the document
    if im.width < 48 or im.height < 24:
        return []
    if im.width * im.height > MAX_PIXELS:  # downscale huge scans instead of skipping them
        f = (MAX_PIXELS / (im.width * im.height)) ** 0.5
        im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    r = _eng(buf.getvalue())
    if not r or r.txts is None:
        return []
    return [(t, float(s)) for t, s in zip(r.txts, r.scores) if s >= MIN_SCORE]


def work(job: dict):
    t0 = time.time()
    st = dict(sha256=job["sha256"], format=job["format"], status="ok", error=None, images_ocrd=0, pages_rendered=0)
    acc: Counter = Counter()
    best: dict = {}

    def add(kind, txts):
        for t, s in txts:
            v = norm(t)
            if v:
                acc[(kind, v)] += 1
                best[(kind, v)] = max(best.get((kind, v), 0.0), s)

    fd, tmp = tempfile.mkstemp(dir=HARVEST_DIR / "tmp_ocr")
    os.close(fd)
    try:
        _s3.download_file(STAGING_BUCKET, job["s3_key"], tmp)
        if job["format"] == "pdf":
            import pymupdf
            pymupdf.TOOLS.mupdf_display_errors(False)
            with pymupdf.open(tmp) as doc:
                seen = set()
                for page in doc:
                    txt = page.get_text("text")
                    imgs = page.get_images(full=True)
                    if imgs and len(txt.strip()) < 20 and st["pages_rendered"] < MAX_PDF_RENDER_PAGES:
                        area = page.rect.width * page.rect.height / 72 / 72  # square inches
                        dpi = int(min(150, (MAX_PIXELS / max(area, 1)) ** 0.5))
                        pix = page.get_pixmap(dpi=dpi)
                        add("ocr.pdf_page", _ocr(pix.tobytes("png")))
                        st["pages_rendered"] += 1
                        continue
                    for im in imgs:
                        xref = im[0]
                        if xref in seen or len(seen) >= MAX_PDF_IMAGES:
                            continue
                        seen.add(xref)
                        try:
                            x = doc.extract_image(xref)
                        except Exception:  # noqa: BLE001
                            continue
                        if x and x.get("width", 0) >= 48 and x.get("height", 0) >= 24:
                            add("ocr.pdf_image", _ocr(x["image"]))
                            st["images_ocrd"] += 1
        else:
            with open(tmp, "rb") as f:
                data = f.read()
            add("ocr.image", _ocr(data, strict=True))
            st["images_ocrd"] += 1
    except NotAnImage as e:
        st.update(status="not_image", error=str(e)[:300])  # e.g. FrontPage _vti_cnf text files named *.gif
    except Exception as e:  # noqa: BLE001
        msg = repr(e)
        st.update(status="auth_error" if "Token" in msg else "error", error=(msg + traceback.format_exc(limit=2))[-600:])
    finally:
        os.unlink(tmp)
    st["seconds"] = round(time.time() - t0, 2)
    return st, [(job["sha256"], k, v, n, best[(k, v)]) for (k, v), n in acc.items()]


def targets() -> list[dict]:
    jobs = pq.read_table(HARVEST_DIR / "jobs.parquet").to_pylist()
    status = {r["sha256"]: r for r in pq.read_table(HARVEST_DIR / "status.parquet",
                                                      columns=["sha256", "images", "scanned_pages"]).to_pylist()}
    out = []
    for j in jobs:
        s = status.get(j["sha256"])
        if j["format"] in IMAGE_FORMATS:
            out.append(j)
        elif j["format"] == "pdf" and s and (s["images"] or s["scanned_pages"]):
            out.append(j)
    return out


def run(workers: int, retry_failed: bool = False, rerun_min_size: int = 0) -> None:
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    (HARVEST_DIR / "tmp_ocr").mkdir(exist_ok=True)
    done = set()
    for p in OCR_DIR.glob("status-*.parquet"):
        done |= {r["sha256"] for r in pq.read_table(p).to_pylist()
                 if r["status"] not in ("auth_error",) + (("error",) if retry_failed else ())}
    todo = [j for j in targets() if j["sha256"] not in done
            or (rerun_min_size and j["format"] != "pdf" and j["size"] >= rerun_min_size)]
    print(f"ocr targets todo={len(todo)} ({Counter(j['format'] for j in todo).most_common(8)})", flush=True)
    rows, sts, t0 = [], [], time.time()

    def flush():
        if not sts:
            return
        tag = f"{len(list(OCR_DIR.glob('status-*.parquet'))):05d}-{int(time.time())}"
        pq.write_table(pa.Table.from_pylist([s for s in sts if s["status"] != "auth_error"], STATUS), OCR_DIR / f"status-{tag}.parquet")
        cols = list(zip(*rows)) if rows else [[]] * 5
        pq.write_table(pa.table([pa.array(c, f.type) for c, f in zip(cols, SCHEMA)], schema=SCHEMA), OCR_DIR / f"strings-{tag}.parquet")
        sts.clear()
        rows.clear()

    with get_context("fork").Pool(workers, initializer=_init, maxtasksperchild=200) as pool:
        for i, (st, r) in enumerate(pool.imap_unordered(work, todo, chunksize=1), 1):
            sts.append(st)
            rows.extend(r)
            if len(sts) >= 500:
                flush()
            if i % 200 == 0:
                print(f"{i}/{len(todo)} {(time.time()-t0)/60:.1f} min", flush=True)
    flush()
    print(f"ocr finished in {(time.time()-t0)/60:.1f} min", flush=True)


def aggregate() -> None:
    st = pa.concat_tables([pq.read_table(p) for p in sorted(OCR_DIR.glob("status-*.parquet"))])
    latest = {}  # retried files: the last status wins
    for i, sha in enumerate(st["sha256"].to_pylist()):
        latest[sha] = i
    st = st.take(sorted(latest.values()))
    import duckdb  # retried files: keep strings from their latest part only (part tags sort chronologically)
    s = duckdb.sql(f"""SELECT * EXCLUDE (filename, last) FROM (
            SELECT *, max(filename) OVER (PARTITION BY sha256) AS last
            FROM read_parquet('{OCR_DIR}/strings-*.parquet', filename=true)) WHERE filename = last""").to_arrow_table()
    pq.write_table(st, HARVEST_DIR / "ocr_status.parquet")
    pq.write_table(s, HARVEST_DIR / "ocr_occurrences.parquet", compression="zstd")
    print(dict(files=st.num_rows, status=Counter(st["status"].to_pylist()), strings=s.num_rows,
               images=sum(st["images_ocrd"].to_pylist()), pages=sum(st["pages_rendered"].to_pylist()),
               cpu_h=round(sum(st["seconds"].to_pylist()) / 3600, 2)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "aggregate"])
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--rerun-min-size", type=int, default=0, help="re-OCR standalone images >= N bytes")
    a = ap.parse_args()
    run(a.workers, a.retry_failed, a.rerun_min_size) if a.cmd == "run" else aggregate()
    sys.exit(0)
