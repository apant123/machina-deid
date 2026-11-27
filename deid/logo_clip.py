"""Stage 3b: whole-image logo / signature classifier (CLIP zero-shot, CPU), cached by image sha256.

Why: OWLv2 finds logos *inside* a scene; an image that *is* a logo (a title-block or footer logo placed
as its own raster in a PDF) gets low, partial boxes (0.10-0.13 on the gear only, on one word only).
A zero-shot whole-image classifier separates those cleanly, and also finds handwritten signatures.

    python -m deid.logo_clip run [--workers 14]    # scores every image of the logo scan -> logos/clip.parquet
    python -m deid.logo_clip sheet                 # contact sheets per score band -> logos/sheets/clip_*.png

The rewrite blanks a whole image when its logo or signature score clears the threshold chosen at the
contact-sheet review (see image.py). Prompts are generic; nothing corpus-specific lives here.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from functools import lru_cache
from multiprocessing import get_context

import pyarrow as pa
import pyarrow.parquet as pq

from .logo_scan import IMG_DIR, LOGO_DIR

MODEL = "openai/clip-vit-base-patch32"
LABELS = {
    "logo": ["a company logo", "an organization logo or emblem", "a brand logo with text", "a university crest"],
    "signature": ["a handwritten signature"],
    "other": ["a photo of a machine part", "a technical drawing", "a 3D CAD rendering", "a software screenshot",
              "a chart or diagram", "a photo of a workshop", "a table of numbers", "a black rectangle",
              "an icon or arrow", "a blank image", "text in a document"],
}
OUT = LOGO_DIR / "clip.parquet"
SCHEMA = pa.schema([("img_sha", pa.string()), ("w", pa.int32()), ("h", pa.int32()), ("logo", pa.float32()),
                    ("signature", pa.float32()), ("other", pa.float32()), ("error", pa.string())])


@lru_cache(maxsize=1)
def _model():
    import torch
    torch.set_num_threads(int(os.environ.get("DEID_TORCH_THREADS", "1")))
    from transformers import CLIPModel, CLIPProcessor
    model, proc = CLIPModel.from_pretrained(MODEL).eval(), CLIPProcessor.from_pretrained(MODEL)
    texts = [t for v in LABELS.values() for t in v]
    with torch.no_grad():
        t = model.get_text_features(**proc(text=texts, return_tensors="pt", padding=True))
        t = getattr(t, "pooler_output", t)
        t = t / t.norm(dim=-1, keepdim=True)
    owner = [k for k, v in LABELS.items() for _ in v]
    return model, proc, t, owner


def classify(images) -> list[dict]:
    """PIL images -> [{'logo': p, 'signature': p, 'other': p}] (softmax over all prompts, summed per label)."""
    import torch
    model, proc, temb, owner = _model()
    with torch.no_grad():
        e = model.get_image_features(**proc(images=[im.convert("RGB") for im in images], return_tensors="pt"))
        e = getattr(e, "pooler_output", e)
        p = (100 * (e / e.norm(dim=-1, keepdim=True)) @ temb.T).softmax(-1)
    out = []
    for row in p.tolist():
        agg = dict.fromkeys(LABELS, 0.0)
        for i, v in enumerate(row):
            agg[owner[i]] += v
        out.append(agg)
    return out


def _batch(hashes: list[str]) -> list[dict]:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    rows, ims = [], []
    for h in hashes:
        try:
            im = Image.open(IMG_DIR / h[:2] / h)
            w, hh = im.size
            im.draft("RGB", (448, 448))
            im = im.convert("RGB")
            im.thumbnail((448, 448))
            rows.append(dict(img_sha=h, w=w, h=hh, error=None))
            ims.append(im)
        except Exception as e:  # noqa: BLE001
            rows.append(dict(img_sha=h, w=0, h=0, logo=None, signature=None, other=None, error=repr(e)[:200]))
    ok = [r for r in rows if r["error"] is None]
    for r, s in zip(ok, classify(ims) if ims else []):
        r.update(s)
    return rows


def run(workers: int) -> None:
    done = set(pq.read_table(OUT, columns=["img_sha"]).column(0).to_pylist()) if OUT.exists() else set()
    todo = sorted(set(pq.read_table(LOGO_DIR / "logo_scan.parquet", columns=["img_sha"]).column(0).to_pylist()) - done)
    print(f"clip: {len(todo)} images to score ({len(done)} cached), workers={workers}", flush=True)
    chunks = [todo[i:i + 32] for i in range(0, len(todo), 32)]
    rows, t0 = (pq.read_table(OUT).to_pylist() if done else []), time.time()
    with get_context("spawn").Pool(workers) as pool:
        for i, part in enumerate(pool.imap_unordered(_batch, chunks), 1):
            rows += part
            if i % 100 == 0 or i == len(chunks):
                pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), OUT)
                print(f"{i * 32}/{len(todo)}  {(time.time() - t0) / 60:.1f} min", flush=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), OUT)
    print("clip finished", flush=True)


# Thresholds chosen at the contact-sheet review (2026-11-27); whole-image blanking only for images of
# at most MAX_PIXELS (a scanned page is never blanked whole; OWLv2 boxes handle logos inside pages).
LOGO_SURE = 0.90       # rule A: CLIP alone (~90% logos on the sheet)
LOGO_LIKELY = 0.80     # rule B: CLIP 0.80-0.90 needs OWLv2 support >= OWL_SUPPORT
OWL_SUPPORT = 0.10
SIGNATURE = 0.90       # rule S, with a minimum side (tiny rotated drawing labels score high too)
SIGNATURE_MIN_SIDE = 60
MAX_PIXELS = 4_000_000


def load_blank_set() -> dict | None:
    """img_sha -> 'signature' | 'logo' for the images the rewrite blanks whole (None = no CLIP pass yet)."""
    if not OUT.exists():
        return None
    owl = {}
    for r in pq.read_table(LOGO_DIR / "logo_scan.parquet").to_pylist():
        try:
            b = json.loads(r["boxes"])
        except (TypeError, ValueError):
            continue
        if isinstance(b, list) and b:
            owl[r["img_sha"]] = max(x[1] for x in b)
    out = {}
    for r in pq.read_table(OUT).to_pylist():
        if r["error"] is not None or r["w"] * r["h"] > MAX_PIXELS:
            continue
        if r["signature"] >= SIGNATURE and min(r["w"], r["h"]) >= SIGNATURE_MIN_SIDE:
            out[r["img_sha"]] = "signature"
        elif r["logo"] >= LOGO_SURE or (r["logo"] >= LOGO_LIKELY and owl.get(r["img_sha"], 0.0) >= OWL_SUPPORT):
            out[r["img_sha"]] = "logo"
    return out


def sheet(per_band: int = 60) -> None:
    """Random contact sheet per score band, for logo and signature (review before choosing thresholds)."""
    from PIL import Image, ImageDraw, ImageFont
    Image.MAX_IMAGE_PIXELS = None
    rows = [r for r in pq.read_table(OUT).to_pylist() if r["error"] is None]
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    (LOGO_DIR / "sheets").mkdir(exist_ok=True)
    bands = [(0.9, 1.01), (0.8, 0.9), (0.7, 0.8), (0.6, 0.7), (0.5, 0.6), (0.4, 0.5), (0.3, 0.4)]
    for label in ("logo", "signature"):
        for lo, hi in bands:
            sel = [r for r in rows if lo <= r[label] < hi]
            random.Random(7).shuffle(sel)
            pick = sel[:per_band]
            cell, cols = 130, 10
            sh = Image.new("RGB", (cols * cell, max(1, (len(pick) + cols - 1) // cols) * (cell + 16)), "white")
            d = ImageDraw.Draw(sh)
            for k, r in enumerate(pick):
                im = Image.open(IMG_DIR / r["img_sha"][:2] / r["img_sha"])
                im.draft("RGB", (256, 256))
                im = im.convert("RGB")
                im.thumbnail((cell - 6, cell - 6))
                x, y = (k % cols) * cell, (k // cols) * (cell + 16)
                sh.paste(im, (x + 3, y + 3))
                d.text((x + 3, y + cell - 1), f"{r[label]:.2f} {r['w']}x{r['h']}", fill="red", font=font)
            sh.save(LOGO_DIR / "sheets" / f"clip_{label}_{lo:.1f}.png")
            print(f"{label} {lo:.1f}-{min(hi, 1):.1f}: {len(sel)} images", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "sheet"])
    ap.add_argument("--workers", type=int, default=14)
    a = ap.parse_args()
    run(a.workers) if a.cmd == "run" else sheet()
