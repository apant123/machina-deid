"""PDF writer (PyMuPDF).

1. Text layer: every span whose text the map changes -> the changed characters are redacted
   (text only: vector line art and images are kept, PDF_REDACT_LINE_ART_NONE / IMAGE_NONE) and the
   pseudonym is drawn at the same baseline, size and colour (Helvetica, or the bundled CJK font).
2. Rasters: embedded images and scanned pages go through the image scrubber (OCR text -> pseudonym,
   logos -> neutral mark); changed images are replaced in place (same xref).
3. Metadata: Info dictionary, XMP, annotations (author / contents), form fields, link URIs, embedded
   files (recursively de-identified through the dispatcher).
4. Object pass: every remaining object and non-image, non-font stream is mapped (literal strings,
   UTF-16 hex strings <FEFF..>, XML / PieceInfo payloads).
5. Saved with garbage collection and no incremental history (old revisions of the text vanish).
"""
from __future__ import annotations

import hashlib

import re
import shutil
from pathlib import Path

from .dispatch import Ctx, Outcome, rewrite_path, temp_dir

_HEX16 = re.compile(r"<(FEFF(?:[0-9A-Fa-f]{4})+)>")


def _is_cjk(s: str) -> bool:
    return any("　" <= c <= "鿿" or "가" <= c <= "힯" or "＀" <= c <= "￯" for c in s)


def _glyph_core(rect, origin, size: float, direction):
    """Redaction box shrunk to the glyph core. Char bboxes in CAD-exported PDFs often span the font's full
    ascender/descender (taller than the line pitch), and MuPDF removes every glyph a redaction touches: a
    replaced word erased letters of the lines above and below ('...ITUTE OF TECHNOLOGY')."""
    import pymupdf
    if abs(direction[1]) > 0.5:      # vertical text: shrink across the line (x), keep the run length (y)
        cx = origin[0]
        return pymupdf.Rect(max(rect.x0, cx - 0.15 * size), rect.y0 + 0.3, min(rect.x1, cx + 0.75 * size), rect.y1 - 0.3)
    y = origin[1]                    # horizontal: a thin strip through the middle of the x-height. MuPDF removes
    #                                  every glyph whose box touches it (all of the run), while the boxes of the
    #                                  lines above / below end at their descender / ascender and never reach it
    return pymupdf.Rect(rect.x0 + 0.3, y - 0.45 * size, rect.x1 - 0.3, y - 0.25 * size)


def _text_layer(doc, ctx: Ctx, info: dict):
    import pymupdf
    for page in doc:
        todo = []
        raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    chars = span.get("chars", [])
                    text = "".join(c["c"] for c in chars)
                    if not text.strip():
                        continue
                    new = ctx.dm.text(text, ctx.st)
                    if new == text or len(new) != len(text):
                        continue
                    # contiguous runs of changed characters
                    i = 0
                    while i < len(text):
                        if text[i] == new[i]:
                            i += 1
                            continue
                        j = i
                        while j < len(text) and (text[j] != new[j] or j + 1 < len(text) and text[j + 1] != new[j + 1]):
                            j += 1
                        # extend the run to whole tokens so the redrawn word is uniform
                        while i > 0 and text[i - 1].isalnum():
                            i -= 1
                        while j < len(text) and text[j].isalnum():
                            j += 1
                        rect = pymupdf.Rect(chars[i]["bbox"])
                        for c in chars[i + 1:j]:
                            rect |= pymupdf.Rect(c["bbox"])
                        rect = _glyph_core(rect, chars[i]["origin"], span["size"], line.get("dir", (1, 0)))
                        todo.append((rect, chars[i]["origin"], new[i:j], span["size"], span.get("color", 0), line.get("dir", (1, 0))))
                        i = j
        if not todo:
            continue
        for rect, *_ in todo:
            page.add_redact_annot(rect)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        for rect, origin, s, size, color, direction in todo:
            rgb = ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255
            font = "china-s" if _is_cjk(s) else "helv"
            fs = size
            w = pymupdf.get_text_length(s, fontname=font, fontsize=fs) if font == "helv" else fs * len(s)
            width = max(rect.width, rect.height) if abs(direction[1]) > 0.5 else rect.width
            if w > width > 0:
                fs = fs * width / w
            rot = 0
            if abs(direction[1]) > 0.5:  # vertical text in drawings
                rot = 90 if direction[1] < 0 else 270
            try:
                page.insert_text(origin, s, fontsize=fs, fontname=font, color=rgb, rotate=rot)
            except Exception:  # noqa: BLE001 - odd rotations: draw unrotated rather than fail the document
                page.insert_text(origin, s, fontsize=fs, fontname=font, color=rgb)
            info["text_replaced"] += 1


def _vector_logos(doc, ctx: Ctx, info: dict):
    """Blank page-region logos / signatures found by vector_logo_scan: inline images (no xref, invisible to the
    image passes) and vector-drawn logos. Image pixels and paths fully inside the box are removed, then the
    neutral mark is drawn (signatures: flat blank). Page-1 vector hits are applied to later pages whose region
    renders the same (repeated title blocks)."""
    import pymupdf
    regs = (ctx.vector_logos or {}).get(ctx.blob_sha) if ctx.depth == 0 else None
    if not regs or not doc.page_count:
        return
    def thumb(page, r):
        pix = page.get_pixmap(clip=r, dpi=36, colorspace=pymupdf.csGRAY)
        return pix.samples, (pix.width, pix.height)
    per_page: dict[int, list] = {}
    for g in regs:
        if g["page"] < doc.page_count:
            per_page.setdefault(g["page"], []).append((pymupdf.Rect(g["rect"]), g["kind"]))
    p0 = doc[0]
    refs = [(pymupdf.Rect(g["rect"]), g["kind"], thumb(p0, pymupdf.Rect(g["rect"]))) for g in regs
            if g.get("propagate") and g["page"] == 0]
    for page in doc:
        hits = list(per_page.get(page.number, []))
        if page.number > 0 and page.rect == p0.rect:
            for r, kind, (t0, size0) in refs:
                t, size = thumb(page, r)
                if size == size0 and t and sum(abs(a - b) for a, b in zip(t, t0)) / len(t) < 12:
                    hits.append((r, kind))
        if not hits:
            continue
        # stamp / free-text annotations draw ON TOP of the page content: a logo stored as a Stamp appearance
        # stream survives the content redaction, so annotations mostly inside a region are removed
        for a in list(page.annots() or []):
            if a.type[0] in (pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET, pymupdf.PDF_ANNOT_REDACT):
                continue
            ar = a.rect
            if ar.is_empty:
                continue
            if any((ar & r).get_area() >= 0.5 * min(ar.get_area(), r.get_area()) for r, _ in hits):
                page.delete_annot(a)
                info["region_annots_removed"] = info.get("region_annots_removed", 0) + 1
        for r, _ in hits:
            page.add_redact_annot(r, fill=(1, 1, 1))
        # pixels of images in the box + paths fully inside it; text is left to the text-layer pass (a logo box
        # often touches the title-block text next to it, and MuPDF removes every glyph a redaction touches)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
        for r, kind in hits:
            if kind == "logo":
                pad = max(2, min(r.width, r.height) / 6)
                page.draw_rect(pymupdf.Rect(r.x0 + pad, r.y0 + pad, r.x1 - pad, r.y1 - pad), color=(0.59, 0.59, 0.59),
                               width=max(0.5, pad / 3), radius=0.2)
                info["region_logos"] = info.get("region_logos", 0) + 1
            else:
                info["region_signatures"] = info.get("region_signatures", 0) + 1


def _images(doc, ctx: Ctx, info: dict):
    import pymupdf
    from .image import scrub_bytes, scrub_pil
    done = set()
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in done:
                continue
            done.add(xref)
            try:
                if img[2] < 48 or img[3] < 24:
                    continue
                # the logo cache is keyed by the PNG of the pixmap (as deid.logo_scan hashes it)
                pix = pymupdf.Pixmap(doc, xref)  # SMask (img[1]) is kept; only the base image is scrubbed
                if pix.n - pix.alpha >= 4:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                png = pix.tobytes("png")
                pix = None
                key = hashlib.sha256(png).hexdigest()
                if img[8] == "DCTDecode" and img[5] in ("DeviceRGB", "DeviceGray"):
                    # JPEG in -> JPEG out: PyMuPDF stores a JPEG replacement as-is, while a PNG one is
                    # decoded and held as raw samples until save (~0.4 GB per 66 MP page: OOM on big scans)
                    data = doc.xref_stream_raw(xref)
                else:
                    data = png
                png = None
            except Exception:  # noqa: BLE001
                info["image_errors"] += 1
                continue
            new, sinfo = scrub_bytes(data, ctx, image_id=f"{info['name']}#x{xref}", img_sha=key)
            info["logo_cache_miss"] += sinfo.get("logo_cache_miss", 0)
            info["ocr_lines"] += sinfo.get("replaced_lines", 0)
            info["logos"] += sinfo.get("logos", 0)
            info["signatures"] += sinfo.get("signatures", 0)
            if new is not data:
                try:
                    page.replace_image(xref, stream=new)
                    info["images_replaced"] += 1
                except Exception:  # noqa: BLE001
                    info["image_errors"] += 1
        # scanned pages (image-only): render, scrub, and overlay where text was replaced
        if len(page.get_text("text").strip()) < 20 and page.get_images():
            area = page.rect.width * page.rect.height / 72 / 72
            dpi = int(min(150, (40_000_000 / max(area, 1)) ** 0.5))
            pix = page.get_pixmap(dpi=dpi)
            from PIL import Image
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            new, sinfo = scrub_pil(im, ctx, f"{info['name']}#p{page.number}", logos=False)
            if new is not im:
                import io
                buf = io.BytesIO()
                new.save(buf, "PNG")
                page.insert_image(page.rect, stream=buf.getvalue(), overlay=True)
                info["pages_overlaid"] += 1
                info["ocr_lines"] += sinfo.get("replaced_lines", 0)


def _meta(doc, ctx: Ctx, info: dict):
    md = doc.metadata or {}
    new = {k: ctx.dm.value(v, ctx.st) if isinstance(v, str) else v for k, v in md.items()}
    if new != md:
        doc.set_metadata({k: v for k, v in new.items() if k in ("author", "title", "subject", "keywords", "creator",
                                                                   "producer", "creationDate", "modDate", "trapped")})
        info["meta"] = True
    xmp = doc.get_xml_metadata()
    if xmp:
        x2 = ctx.dm.text(xmp, ctx.st)
        if x2 != xmp:
            doc.set_xml_metadata(x2)
    for page in doc:
        for a in page.annots() or []:
            i = a.info
            n = {k: ctx.dm.value(v, ctx.st) if isinstance(v, str) else v for k, v in i.items()}
            if n != i:
                a.set_info(n)
                a.update()
                info["annots"] += 1
        for w in page.widgets() or []:
            changed = False
            for attr in ("field_value", "field_label", "field_name"):
                v = getattr(w, attr, None)
                if isinstance(v, str) and (nv := ctx.dm.value(v, ctx.st)) != v:
                    setattr(w, attr, nv)
                    changed = True
            if changed:
                w.update()
                info["fields"] += 1
        for ln in page.get_links():
            if ln.get("uri") and (nu := ctx.dm.text(ln["uri"], ctx.st)) != ln["uri"]:
                ln["uri"] = nu
                try:
                    page.update_link(ln)
                    info["links"] += 1
                except Exception:  # noqa: BLE001 - link without its own object ('bad xref'): its /URI string is
                    info["link_errors"] = info.get("link_errors", 0) + 1  # rewritten by _objects (page / annot dicts)


def _embedded(doc, ctx: Ctx, info: dict) -> bool:
    work = temp_dir(ctx)
    try:
        for i, name in enumerate(doc.embfile_names()):
            data = doc.embfile_get(name)
            meta = doc.embfile_info(name)
            fn = meta.get("filename") or name
            src, dst = work / f"e{i}", work / f"e{i}.out"
            src.write_bytes(data)
            o = rewrite_path(src, dst, fn, ctx.child())
            if o.status not in ("ok", "copy"):
                info["error"] = f"embedded file quarantined: {fn}"
                return False
            # PyMuPDF 2.1 embfile_upd crashes when given content (bytes has no .m_internal): rename via
            # embfile_upd without content, then write the stream ourselves and fix its size keys
            xref = doc.embfile_upd(name, filename=ctx.dm.key(fn), ufilename=ctx.dm.key(meta.get("ufilename") or fn),
                                   desc=ctx.dm.text(meta.get("description") or "", ctx.st))
            new = dst.read_bytes()
            doc.update_stream(xref, new, compress=True)
            doc.xref_set_key(xref, "DL", str(len(new)))
            doc.xref_set_key(xref, "Params/Size", str(len(new)))
            info["embedded"] += 1
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _objects(doc, ctx: Ctx, info: dict):
    """Everything not covered above: object dictionaries and non-image / non-font / non-content streams."""
    content = set()
    for page in doc:
        content.update(page.get_contents())
    for xref in range(1, doc.xref_length()):
        try:
            src = doc.xref_object(xref, compressed=False)
        except Exception:  # noqa: BLE001
            continue
        if not src or src == "null":
            continue
        new = ctx.dm.text(src, ctx.st)

        def hexsub(m):
            raw = bytes.fromhex(m.group(1)[4:]).decode("utf-16-be", "replace")
            r = ctx.dm.text(raw, ctx.st)
            return m.group(0) if r == raw else "<FEFF" + r.encode("utf-16-be").hex().upper() + ">"
        new = _HEX16.sub(hexsub, new)
        if new != src:
            try:
                doc.update_object(xref, new)
                info["objects"] += 1
            except Exception:  # noqa: BLE001
                info["object_errors"] += 1
        if doc.xref_is_stream(xref) and xref not in content and not re.search(r"/Subtype\s*/(Image|Type1C|CIDFontType0C|OpenType)|/Length[123]|/FontFile", src):
            try:
                data = doc.xref_stream(xref)
            except Exception:  # noqa: BLE001
                continue
            if data and len(data) < 50_000_000:
                nd = ctx.dm.apply(data, ctx.st)
                if nd != data:
                    doc.update_stream(xref, nd)
                    info["streams"] += 1


def rewrite_pdf(src: Path, dst: Path, ctx: Ctx) -> Outcome:
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    info = dict(name=src.name, text_replaced=0, images_replaced=0, pages_overlaid=0, ocr_lines=0, logos=0, meta=False,
                annots=0, fields=0, links=0, embedded=0, objects=0, object_errors=0, streams=0, image_errors=0, logo_cache_miss=0,
                signatures=0)
    try:
        doc = pymupdf.open(src)
    except Exception as e:  # noqa: BLE001
        return Outcome("quarantine", "pdf", dict(error=f"open: {e!r}"[:200]))
    if doc.needs_pass or doc.is_encrypted:
        return Outcome("quarantine", "pdf", dict(error="encrypted pdf"))
    with doc:
        _text_layer(doc, ctx, info)
        _vector_logos(doc, ctx, info)
        _images(doc, ctx, info)
        _meta(doc, ctx, info)
        if not _embedded(doc, ctx, info):
            return Outcome("quarantine", "pdf", info)
        _objects(doc, ctx, info)
        doc.save(dst, garbage=3, deflate=True, clean=False, incremental=False)
    # residual: text layer + metadata of the result
    res = []
    with pymupdf.open(dst) as d2:
        txt = "\n".join(p.get_text("text") for p in d2) + "\n" + str(d2.metadata) + (d2.get_xml_metadata() or "")
        res = ctx.dm.passes[0].residual_text(txt)
    return Outcome("ok", "pdf", info, residual=res)
