"""Raster scrubber: standalone images, images embedded in PDFs, scanned PDF pages.

1. OCR (RapidOCR, ONNX, CPU): every text line whose text the pseudonym map changes is painted over
   with its local background colour and the pseudonym is drawn in its place (same box, fitted size,
   same ink colour). CJK uses the CJK font bundled with PyMuPDF.
2. Logos (OWLv2 open-vocabulary detector, CPU): boxes scoring >= LOGO_THRESHOLD for "logo" queries
   are replaced by a neutral mark (flat background + plain outline). Detections are logged so they
   can be reviewed on a contact sheet before the full run.
3. Metadata (EXIF / XMP / PNG text chunks) is dropped whenever the image is re-encoded, and mapped
   in place otherwise.
An image is only re-encoded when something changed; untouched images keep their original bytes.
"""
from __future__ import annotations

import io
import os
from functools import lru_cache
from pathlib import Path

from .dispatch import Ctx, Outcome

LOGO_QUERIES = ["a company logo", "a brand logo", "a logo"]
OWL_SIZE = int(os.environ.get("DEID_OWL_SIZE", "768"))  # 960 native; 768 = 2x faster, same logo/drawing separation
LOGO_THRESHOLD = float(os.environ.get("DEID_LOGO_THRESHOLD", "0.14"))  # chosen at the contact-sheet review (callouts filtered)
MIN_SIDE = 24
MAX_PIXELS = 40_000_000
LOGO_LOG: list = []  # (image id, box, score) for the review contact sheet


# ------------------------------------------------------------------ engines (lazy, per process)
@lru_cache(maxsize=1)
def _ocr():
    import logging
    import cv2
    cv2.setNumThreads(1)
    from rapidocr import RapidOCR
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    return RapidOCR(params={"Global.log_level": "error", "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                            "EngineConfig.onnxruntime.inter_op_num_threads": 1})


@lru_cache(maxsize=1)
def _owl():
    import torch
    torch.set_num_threads(int(os.environ.get("DEID_TORCH_THREADS", "1")))
    from transformers import Owlv2ForObjectDetection, Owlv2Processor
    name = "google/owlv2-base-patch16-ensemble"
    return Owlv2Processor.from_pretrained(name), Owlv2ForObjectDetection.from_pretrained(name).eval()


@lru_cache(maxsize=4)
def _font(cjk: bool, size: int):
    from PIL import ImageFont
    if cjk:
        import pymupdf
        p = Path(os.environ.get("DEID_TMP", "/tmp")) / "deid_cjk.ttf"
        if not p.exists():
            p.write_bytes(pymupdf.Font("china-s").buffer)
        return ImageFont.truetype(str(p), size)
    return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)


# ------------------------------------------------------------------ helpers
def _bg_ink(im, box):
    """Background = median of the box border; ink = the pixel farthest from it inside the box."""
    import numpy as np
    x0, y0, x1, y1 = box
    a = np.asarray(im.convert("RGB"))[max(0, y0):y1, max(0, x0):x1].reshape(-1, 3)
    if not len(a):
        return (255, 255, 255), (0, 0, 0)
    sub = np.asarray(im.convert("RGB"))
    ring = np.concatenate([sub[max(0, y0 - 2):y0 + 1, x0:x1].reshape(-1, 3), sub[y1 - 1:y1 + 2, x0:x1].reshape(-1, 3),
                           sub[y0:y1, max(0, x0 - 2):x0 + 1].reshape(-1, 3), sub[y0:y1, x1 - 1:x1 + 2].reshape(-1, 3)])
    bg = tuple(int(v) for v in np.median(ring if len(ring) else a, axis=0))
    d = np.abs(a.astype(int) - np.array(bg)).sum(1)
    ink = tuple(int(v) for v in a[int(d.argmax())])
    return bg, ink


def _draw_text(im, box, text: str):
    from PIL import ImageDraw
    x0, y0, x1, y1 = box
    bg, ink = _bg_ink(im, box)
    dr = ImageDraw.Draw(im)
    dr.rectangle([x0, y0, x1, y1], fill=bg)
    cjk = any("　" <= c <= "鿿" or "가" <= c <= "힯" for c in text)
    h = max(6, int((y1 - y0) * 0.85))
    for size in range(h, 5, -1):
        f = _font(cjk, size)
        w = dr.textlength(text, font=f)
        if w <= (x1 - x0) or size == 6:
            break
    dr.text((x0, y0 + max(0, ((y1 - y0) - size) // 2)), text, fill=ink, font=f)


def _neutral_mark(im, box):
    from PIL import ImageDraw
    x0, y0, x1, y1 = box
    bg, ink = _bg_ink(im, box)
    dr = ImageDraw.Draw(im)
    dr.rectangle([x0, y0, x1, y1], fill=bg)
    pad = max(2, min(x1 - x0, y1 - y0) // 6)
    dr.rounded_rectangle([x0 + pad, y0 + pad, x1 - pad, y1 - pad], radius=pad, outline=(150, 150, 150), width=max(1, pad // 3))


def detect_logos(im) -> list[tuple[tuple[int, int, int, int], float]]:
    import torch
    proc, model = _owl()
    rgb = im.convert("RGB")
    with torch.no_grad():
        inp = proc(text=[LOGO_QUERIES], images=rgb, return_tensors="pt", size={"height": OWL_SIZE, "width": OWL_SIZE})
        out = model(**inp, interpolate_pos_encoding=OWL_SIZE != 960)
    # OWLv2 pads to a square: boxes are relative to the padded square
    side = max(rgb.size)
    res = proc.post_process_grounded_object_detection(out, threshold=LOGO_THRESHOLD, target_sizes=[(side, side)])[0]
    boxes = []
    for b, s in zip(res["boxes"].tolist(), res["scores"].tolist()):
        x0, y0, x1, y1 = (int(max(0, v)) for v in b)
        x1, y1 = min(x1, rgb.width), min(y1, rgb.height)
        area = (x1 - x0) * (y1 - y0)
        if x1 - x0 >= 8 and y1 - y0 >= 8 and area < 0.9 * rgb.width * rgb.height:  # not the whole picture
            boxes.append(((x0, y0, x1, y1), float(s)))
    return boxes


def is_callout(crop) -> bool:
    """Drafting callout (circled single letter / digit, monochrome on light paper): OWLv2's main false positive.

    Calibrated on the review contact sheet: ~95% of what this rejects are section/detail/datum callouts.
    """
    import cv2
    import numpy as np
    w, h = crop.size
    if w < 16 or h < 16 or not (0.7 <= w / h <= 1.43):
        return False
    if np.asarray(crop.convert("HSV"))[..., 1].mean() > 25:  # colourful -> not a drafting symbol
        return False
    g = np.asarray(crop.convert("L"))
    if ((g > 60) & (g < 195)).mean() > 0.12 or g.mean() < 128:  # photo / gradient / dark-background mark
        return False
    s = min(w, h)
    c = cv2.HoughCircles(cv2.medianBlur(g, 3), cv2.HOUGH_GRADIENT, dp=1, minDist=s, param1=100,
                         param2=int(max(15, s * 0.25)), minRadius=int(s * 0.30), maxRadius=int(s * 0.62))
    if c is None:
        return False
    x, y, _ = c[0][0]
    return abs(x - w / 2) < 0.2 * s and abs(y - h / 2) < 0.2 * s


def logo_boxes(work, ctx: Ctx, img_sha: str | None):
    """Cached detections (logo scan pass) when available; boxes are in `work` coordinates. Callouts dropped."""
    if ctx.logo_cache is not None and img_sha is not None:
        boxes = [(tuple(b), s) for b, s in ctx.logo_cache.get(img_sha, []) if s >= LOGO_THRESHOLD]
    else:
        boxes = detect_logos(work)
    if not boxes:
        return boxes
    rgb = work.convert("RGB")
    return [(b, s) for b, s in boxes if not is_callout(rgb.crop(b))]


def scrub_pil(im, ctx: Ctx, image_id: str = "", logos: bool = True, img_sha: str | None = None) -> tuple[object, dict]:
    """-> (image, info). info: ocr_lines, replaced_lines, logos."""
    import numpy as np
    info = dict(ocr_lines=0, replaced_lines=0, logos=0)
    if im.width < MIN_SIDE or im.height < MIN_SIDE:
        return im, info
    work = im
    if im.width * im.height > MAX_PIXELS:
        f = (MAX_PIXELS / (im.width * im.height)) ** 0.5
        work = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))))
    scale = im.width / work.width
    rgb = work.convert("RGB")
    r = _ocr()(np.asarray(rgb)[:, :, ::-1].copy())
    out = None
    if r is not None and r.txts is not None:
        for poly, txt, sc in zip(r.boxes, r.txts, r.scores):
            info["ocr_lines"] += 1
            new = ctx.dm.text(txt, ctx.st)
            if new == txt:
                continue
            xs, ys = [p[0] * scale for p in poly], [p[1] * scale for p in poly]
            box = (int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1)
            if out is None:
                out = im.convert("RGB")
            _draw_text(out, box, new)
            info["replaced_lines"] += 1
    if logos and min(im.size) >= 32:
        for box, s in logo_boxes(work, ctx, img_sha):
            b = tuple(int(v * scale) for v in box)
            if out is None:
                out = im.convert("RGB")
            LOGO_LOG.append((image_id, b, round(s, 3)))
            _neutral_mark(out, b)
            info["logos"] += 1
    return (out if out is not None else im), info


def scrub_bytes(data: bytes, ctx: Ctx, image_id: str = "", logos: bool = True) -> tuple[bytes, dict]:
    """Returns the original bytes when nothing was found, else a re-encoded image (metadata dropped)."""
    from PIL import Image, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = 600_000_000
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        fmt = (im.format or "PNG").upper()
    except (UnidentifiedImageError, OSError, ValueError):
        return data, dict(error="unreadable image")
    import hashlib
    new, info = scrub_pil(im, ctx, image_id, logos, hashlib.sha256(data).hexdigest())
    if new is im:
        return data, info
    buf = io.BytesIO()
    save_fmt = fmt if fmt in ("PNG", "JPEG", "TIFF", "BMP", "GIF") else "PNG"
    if save_fmt == "JPEG" and new.mode not in ("RGB", "L"):
        new = new.convert("RGB")
    if save_fmt == "GIF":
        new = new.convert("P")
    new.save(buf, save_fmt, **({"quality": 92} if save_fmt == "JPEG" else {}))
    return buf.getvalue(), info


def _meta_clean(data: bytes, ctx: Ctx) -> bytes:
    """Map text metadata in place (length-preserving) when the pixels are untouched."""
    return ctx.dm.apply(data, ctx.st)


def rewrite_image(src: Path, dst: Path, fmt: str, ctx: Ctx) -> Outcome:
    data = src.read_bytes()
    new, info = scrub_bytes(data, ctx, image_id=src.name)
    if new is data:
        new = _meta_clean(data, ctx)
    dst.write_bytes(new)
    return Outcome("ok", fmt, info)
