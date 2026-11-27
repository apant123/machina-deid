"""Stage 4: page-region logos in output PDFs that the image passes never see.

1. Inline images (BI ... ID ... EI in the content stream) have no xref, so the image passes (OCR, OWLv2, CLIP)
   skipped them: a title-block logo stored inline shipped untouched. Every inline image placement on every
   page is rendered and classified with the CLIP whole-image classifier (same rules as logo_clip: logo >= 0.90,
   signature >= 0.90 with a shortest side >= 60 px).
2. Vector-drawn logos (paths / glyph outlines): OWLv2 on the page-1 render; boxes that do not sit on an
   xref image are confirmed with CLIP (>= CLIP_MIN). Page-1 hits are propagated by the writer to later pages
   whose region renders the same (repeated title blocks).

The PDF writer blanks the regions (pixels of images in the box, paths fully inside it) and draws the
neutral mark; signatures become a flat blank.

    python -m deid.vector_logo_scan run [--workers 12]   # -> logos/vector_scan.parquet
    python -m deid.vector_logo_scan sheet                # contact sheet of confirmed regions
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from multiprocessing import TimeoutError as mp_TimeoutError, get_context

import pyarrow as pa
import pyarrow.parquet as pq

from .logo_scan import LOGO_DIR

OUT = LOGO_DIR / "vector_scan.parquet"
INFLIGHT = LOGO_DIR / "vector_scan_inflight"
DIED = "worker died (out of memory?)"
RESULT_TIMEOUT = 900  # s without any result: assume the pool lost a worker (multiprocessing.Pool then waits forever)
DPI = 100
MAX_PIXELS = 24_000_000
MAX_SIDE = 4000
OWL_MIN = 0.14
CLIP_MIN = 0.90          # vector logos (OWLv2 box + CLIP); 0.80-0.90 held back until a contact-sheet review
INLINE_LOGO = 0.90       # inline images: same thresholds as logo_clip rules A / S
INLINE_SIGNATURE = 0.90
SIGNATURE_MIN_SIDE = 60
INLINE_BATCH = 32
WORKER_MEM_LIMIT = 12 << 30  # address-space cap per worker: a runaway PDF fails alone instead of OOM-killing (and hanging) the pool
_s3 = None


def _init():
    global _s3
    import resource
    import boto3
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEM_LIMIT, WORKER_MEM_LIMIT))
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    _s3 = boto3.client("s3")
    os.environ.setdefault("DEID_TORCH_THREADS", "1")


def _overlap(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))


def _render(page, clip=None):
    """DPI, lowered for huge pages / regions so a render stays under MAX_PIXELS (posters: 44k x 44k px at 100 dpi)."""
    from PIL import Image
    r = clip if clip is not None else page.rect
    dpi = min(DPI, int(72 * (MAX_PIXELS / max(1.0, r.width * r.height)) ** 0.5))
    pix = page.get_pixmap(dpi=max(dpi, 5), clip=clip)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def scan_page(page, vector: bool) -> list[dict]:
    """Candidates on one page: {'page', 'rect' (PDF points), 'src': 'inline'|'vector', 'owl', 'logo', 'signature'}."""
    import pymupdf
    from .logo_clip import classify
    from .rewrite.image import detect_logos, is_callout
    W, H = page.rect.width, page.rect.height
    infos = page.get_image_info(xrefs=True)
    out = []
    inline = [tuple(i["bbox"]) for i in infos if i.get("xref", 0) == 0]
    inline = [r for r in inline if (r[2] - r[0]) >= 8 and (r[3] - r[1]) >= 6 and (r[2] - r[0]) * (r[3] - r[1]) < 0.25 * W * H]
    for i in range(0, len(inline), INLINE_BATCH):  # render per batch: pages can hold thousands of inline images
        crops = [(r, _render(page, pymupdf.Rect(r) & page.rect)) for r in inline[i:i + INLINE_BATCH]]
        part = [(r, c) for r, c in crops if c.width >= 16 and c.height >= 12]
        if part:
            for (r, c), p in zip(part, classify([c for _, c in part])):
                if p["logo"] >= 0.5 or p["signature"] >= 0.5:
                    out.append(dict(page=page.number, rect=[round(v, 1) for v in r], src="inline", owl=0.0,
                                    logo=round(p["logo"], 3), signature=round(p["signature"], 3),
                                    w=c.width, h=c.height))
    if vector:
        im = _render(page)
        if max(im.size) > MAX_SIDE:  # OWLv2 pads to a square: a 25k x 900 px strip would become 25k x 25k
            im.thumbnail((MAX_SIDE, MAX_SIDE))
        f = page.rect.width / im.width  # points per pixel (the dpi may have been lowered)
        xref_rects = [tuple(i["bbox"]) for i in infos if i.get("xref", 0) > 0]
        for (x0, y0, x1, y1), s in detect_logos(im):
            if s < OWL_MIN:
                continue
            r = (x0 * f, y0 * f, x1 * f, y1 * f)
            if any(_overlap(r, ir) > 0.3 for ir in xref_rects) or any(_overlap(r, ir) > 0.5 for ir in inline):
                continue  # xref images: image passes; inline images: classified above
            if (x1 - x0) * (y1 - y0) > 0.25 * im.width * im.height:
                continue
            crop = im.crop((x0, y0, x1, y1))
            p = classify([crop])[0]
            if is_callout(crop) and p["logo"] < 0.95:
                continue
            out.append(dict(page=page.number, rect=[round(v, 1) for v in r], src="vector", owl=round(s, 3),
                            logo=round(p["logo"], 3), signature=round(p["signature"], 3), w=crop.width, h=crop.height))
    return out


def scan_doc(doc) -> list[dict]:
    out = []
    for page in doc:
        out += scan_page(page, vector=page.number == 0)
    return out


def _one(job):
    import pymupdf
    from .config import OUTPUT_BUCKET
    sha, key = job
    INFLIGHT.mkdir(parents=True, exist_ok=True)
    (INFLIGHT / str(os.getpid())).write_text(json.dumps([sha, key]))
    fd, p = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        _s3.download_file(OUTPUT_BUCKET, key, p)
        with pymupdf.open(p) as d:
            return dict(sha256=sha, key=key, cands=json.dumps(scan_doc(d)), error=None)
    except Exception as e:  # noqa: BLE001
        return dict(sha256=sha, key=key, cands="[]", error=repr(e)[:200])
    finally:
        os.unlink(p)
        (INFLIGHT / str(os.getpid())).unlink(missing_ok=True)


def _died() -> list[dict]:
    """Jobs whose worker process no longer exists (e.g. OOM-killed): recorded as errors so a resume skips them."""
    out = []
    for f in INFLIGHT.glob("*") if INFLIGHT.exists() else []:
        if not os.path.exists(f"/proc/{f.name}"):
            sha, key = json.loads(f.read_text())
            out.append(dict(sha256=sha, key=key, cands="[]", error=DIED))
            f.unlink()
    return out


def run(workers: int, only: set | None = None, resume: bool = False) -> None:
    """resume: keep the rows already scanned without error (e.g. before an expired AWS session) and scan the rest."""
    import duckdb
    from .rewrite_run import MANIFEST_DIR, RW_DIR
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    jobs = con.execute(f"""SELECT s.sha256, k.new_key FROM (SELECT sha256, status, format FROM read_parquet('{RW_DIR}/parts/status-*.parquet', filename=true)
            QUALIFY row_number() OVER (PARTITION BY sha256 ORDER BY status='error', filename DESC)=1) s
        JOIN read_parquet('{MANIFEST_DIR}/blobs.parquet') b USING (sha256) JOIN read_parquet('{RW_DIR}/keys.parquet') k ON k.key = b.canonical_key
        WHERE s.status='ok' AND s.format LIKE 'pdf%'""").fetchall()
    prior = [r for r in pq.read_table(OUT).to_pylist() if not r["error"] or r["error"] == DIED] if resume and OUT.exists() else []
    seen, uniq = {r["sha256"] for r in prior}, []
    for h, k in sorted(jobs):
        if h not in seen and (not only or h in only):
            seen.add(h)
            uniq.append((h, k))
    print("pdfs:", len(uniq), "(kept from a previous run:", len(prior), ")", flush=True)
    rows = list(prior)
    hung = False
    with get_context("spawn").Pool(workers, initializer=_init, maxtasksperchild=50) as pool:
        it = pool.imap_unordered(_one, uniq, chunksize=1)
        for i in range(1, len(uniq) + 1):
            try:
                rows.append(it.next(timeout=RESULT_TIMEOUT))
            except mp_TimeoutError:
                hung = True
                break
            if i % 100 == 0:
                print(i, flush=True)
                pq.write_table(pa.Table.from_pylist(rows), OUT)
        if hung:
            pool.terminate()
    dead = _died()
    rows += dead
    pq.write_table(pa.Table.from_pylist(rows), OUT)
    if hung:
        print(f"pool stalled; {len(dead)} job(s) of dead workers recorded as errors; re-run with --resume", flush=True)
    c = [x for r in rows for x in json.loads(r["cands"])]
    conf = confirmed()
    print(dict(pdfs=len(rows), errors=sum(1 for r in rows if r["error"]), candidates=len(c),
               confirmed_pdfs=len(conf), confirmed_regions=sum(len(v) for v in conf.values())))


def _decide(c: dict) -> str | None:
    if c["src"] == "inline":
        if c["signature"] >= INLINE_SIGNATURE and min(c["w"], c["h"]) >= SIGNATURE_MIN_SIDE:
            return "signature"
        if c["logo"] >= INLINE_LOGO:
            return "logo"
        return None
    return "logo" if c["logo"] >= CLIP_MIN else None


def confirmed() -> dict:
    """sha256 -> [{'page', 'rect', 'kind', 'propagate'}] regions the PDF writer blanks."""
    if not OUT.exists():
        return {}
    out = {}
    for r in pq.read_table(OUT).to_pylist():
        regs = []
        for c in json.loads(r["cands"]):
            kind = _decide(c)
            if kind:
                regs.append(dict(page=c["page"], rect=c["rect"], kind=kind, propagate=c["src"] == "vector"))
        if regs:
            out[r["sha256"]] = regs
    return out


def sheet(n: int = 200) -> None:
    import random
    import boto3
    import pymupdf
    from PIL import Image, ImageDraw, ImageFont
    from .config import OUTPUT_BUCKET
    s3 = boto3.client("s3")
    rows = pq.read_table(OUT).to_pylist()
    items = [(r["key"], c) for r in rows for c in json.loads(r["cands"]) if _decide(c)]
    random.Random(3).shuffle(items)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    tiles = []
    for key, c in items[:n]:
        fd, p = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            s3.download_file(OUTPUT_BUCKET, key, p)
            with pymupdf.open(p) as d:
                x0, y0, x1, y1 = c["rect"]
                pix = d[c["page"]].get_pixmap(clip=pymupdf.Rect(x0 - 6, y0 - 6, x1 + 6, y1 + 6), dpi=110)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                im.thumbnail((150, 100))
                t = Image.new("RGB", (156, 120), "white")
                t.paste(im, (3, 3))
                ImageDraw.Draw(t).text((3, 106), f"{c['src'][0]} {_decide(c)[0]} L{c['logo']:.2f} S{c['signature']:.2f}",
                                       fill="red", font=font)
                tiles.append(t)
        except Exception:  # noqa: BLE001
            pass
        finally:
            os.unlink(p)
    cols = 10
    sh = Image.new("RGB", (cols * 156, max(1, (len(tiles) + cols - 1) // cols) * 120), "white")
    for i, t in enumerate(tiles):
        sh.paste(t, ((i % cols) * 156, (i // cols) * 120))
    (LOGO_DIR / "sheets").mkdir(exist_ok=True)
    sh.save(LOGO_DIR / "sheets" / "page_regions.png")
    print("tiles", len(tiles), "of", len(items))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "sheet"])
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--resume", action="store_true", help="keep rows scanned without error, scan the rest")
    a = ap.parse_args()
    run(a.workers, resume=a.resume) if a.cmd == "run" else sheet()
