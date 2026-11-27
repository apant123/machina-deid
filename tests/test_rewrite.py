"""Stage 3 writers: synthetic fixtures only (no corpus data)."""
import io
import struct
import zipfile
import zlib

from deid.replace import Replacer, Stats
from deid.rewrite.mapping import DeidMap


def _dm(**scoped):
    rows = []
    for scope, pairs in scoped.items():
        rows += [dict(surface=k, pseudonym=v, scope=scope, widths_ok=True) for k, v in pairs.items()]
    return DeidMap(rows)


DM = _dm(**{"global": {"Mira Talvenk": "Ines Fintaro", "Quorvex": "Bandulo", "ZORB": "QAXE", "周某某": "林红芳",
                       "Partsvault": "Gimbeloran", "XQZ3D": "WRT3L"},
            "field": {"Zephyn": "Corval"}, "path": {"rje": "gkd"}})


def test_scopes_global_whole_value_and_path_component():
    out = DM.apply(b"C:\\Users\\rje\\a.stp 'Zephyn' 'Zephyn rocks' rje Mira Talvenk")
    assert out == b"C:\\Users\\gkd\\a.stp 'Corval' 'Zephyn rocks' rje Ines Fintaro"


def test_soft_boundaries_one_side_only():
    assert DM.apply(b"\\PartsvaultStandard\\") == b"\\GimbeloranStandard\\"     # CamelCase end, hard start
    assert DM.apply(b"A3XQZ3D-M01") == b"A3WRT3L-M01"                          # digit->letter start, hard end
    assert DM.apply(b"\\XQZ3DPaste-M01") == b"\\WRT3LPaste-M01"            # hard start, acronym->Camel end
    assert DM.apply(b"A3XQZ3DPaste-M01") == b"A3XQZ3DPaste-M01"                # soft on both sides: left alone
    assert DM.apply(b"moQuorvexUnits") == b"moQuorvexUnits"                    # soft on both sides: identifier
    assert DM.apply(b" Quorvexes ") == b" Banduloes "                          # plural


def test_short_key_rules():
    assert DM.apply(b"\x02ZORB3_006_G\x00") == b"\x02QAXE3_006_G\x00"          # name + digits + separator
    assert DM.apply(b"\x00\x00\x17\x00\x00\x00ZORB_TK_04_09_Rail\x00") == b"\x00\x00\x17\x00\x00\x00QAXE_TK_04_09_Rail\x00"
    assert DM.apply(b"\x9a\x12zorbx\x88") == b"\x9a\x12zorbx\x88"              # inside binary noise: untouched


def test_key_rename_matches_in_file_reference():
    key = "sources/github/rje__Quorvex-robot/parts/Quorvex_arm.SLDPRT"
    new = DM.key(key)
    assert new == "sources/github/gkd__Bandulo-robot/parts/Bandulo_arm.SLDPRT"
    ref = DM.apply(b"C:\\work\\Quorvex-robot\\parts\\Quorvex_arm.SLDPRT").decode()
    assert ref.endswith(new.split("/", 3)[3].split("/", 1)[1].replace("/", "\\"))


def test_step_escapes_reencoded_same_length(tmp_path):
    from deid.rewrite.text import rewrite_text
    x2 = "".join(f"{ord(c):04X}" for c in "周某某").encode()
    src = tmp_path / "a.stp"
    src.write_bytes(b"ISO-10303-21;\nDATA;\n#1=PRODUCT('W \\X2\\" + x2 + b"\\X0\\','Mira Talvenk','',(#2));\n"
                    b"#2=CARTESIAN_POINT('',(0.,1.,2.));\nENDSEC;\n")
    dst = tmp_path / "b.stp"
    rewrite_text("step", src, dst, DM, Stats())
    out = dst.read_bytes()
    assert len(out) == src.stat().st_size and b"Ines Fintaro" in out and b"CARTESIAN_POINT('',(0.,1.,2.))" in out
    assert "".join(f"{ord(c):04X}" for c in "林红芳").encode() in out


def test_iges_hollerith_author_is_a_whole_value(tmp_path):
    from deid.rewrite.text import rewrite_text
    g = "1H,,1H;,6Hpart01,6Hpart01,6HZephyn;".ljust(72)
    src = tmp_path / "a.igs"
    src.write_bytes(("S" * 0 + "x".ljust(72) + "S      1\n" + g + "G      1\n").encode())
    dst = tmp_path / "b.igs"
    rewrite_text("iges", src, dst, DM, Stats())
    assert b"6HCorval;" in dst.read_bytes() and len(dst.read_bytes()) == src.stat().st_size


def test_neutral_images_keep_exact_size():
    from PIL import Image
    from deid.rewrite.thumbs import neutral_image
    for fmt in ("PNG", "JPEG", "BMP"):
        buf = io.BytesIO()
        Image.effect_noise((120, 80), 60).convert("RGB").save(buf, fmt)
        orig = buf.getvalue()
        new = neutral_image(orig)
        assert new is not None and len(new) == len(orig) and new != orig
        Image.open(io.BytesIO(new)).load()


def test_sw_deflate_exact_any_target():
    import random
    from deid.rewrite.sw import deflate_exact
    rnd = random.Random(7)  # seeded: a rare random section has an unreachable target (-> quarantine in production)
    data = b"".join(b"swPath=C:\\Users\\someone\\proj\\" + rnd.randbytes(8) for _ in range(160))  # section-like entropy
    base = len(zlib.compress(data)[2:-4])
    for t in (base, base + 1, base + 2, base + 3, base + 4, base + 13):
        out = deflate_exact(data, t)
        assert out is not None and len(out) == t
        assert zlib.decompressobj(-15).decompress(out) == data


def test_zip_container_members_and_names_rewritten(tmp_path):
    from deid.rewrite.dispatch import Ctx, rewrite_path
    src = tmp_path / "a.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Quorvex/notes.txt", "drawn by Mira Talvenk\n")
        z.writestr("Quorvex/part.step", "ISO-10303-21;\nDATA;\n#1=PRODUCT('Quorvex','','',(#2));\nENDSEC;\n")
    dst = tmp_path / "b.zip"
    o = rewrite_path(src, dst, "a.zip", Ctx(DM, logo_cache={}, tmp=tmp_path))
    assert o.status == "ok"
    with zipfile.ZipFile(dst) as z:
        assert z.namelist() == ["Bandulo/notes.txt", "Bandulo/part.step"]
        assert b"Ines Fintaro" in z.read("Bandulo/notes.txt") and b"'Bandulo'" in z.read("Bandulo/part.step")


def test_hostnames_use_substring_rule():
    assert DM.apply(b"Hostname: 'QUORVEXDESKTOP'") == b"Hostname: 'BANDULODESKTOP'"


def test_residual_word_start_rule():
    r = Replacer({"teodo": "Abcde", "demp": "Qwer"})
    assert r.residual_text("C:\\9900417_Teodorico\\x") == ["teodo"]
    assert r.residual_text("isRedemption") == []


def test_drawing_callout_is_not_a_logo():
    from PIL import Image, ImageDraw, ImageFont
    from deid.rewrite.image import is_callout
    im = Image.new("RGB", (120, 120), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((10, 10, 110, 110), outline="black", width=6)
    d.ellipse((20, 20, 100, 100), outline="black", width=3)
    d.text((45, 30), "B", fill="black", font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 50))
    assert is_callout(im)
    logo = Image.new("RGB", (120, 120), (200, 30, 30))
    ImageDraw.Draw(logo).ellipse((10, 10, 110, 110), outline="white", width=6)
    assert not is_callout(logo)


def test_logo_and_model_letter_suffixes():
    assert DM.apply(b"\xe2\xe2ZORB_LOGO\x00\x01zorb_logo.sym\x00") == b"\xe2\xe2QAXE_LOGO\x00\x01qaxe_logo.sym\x00"
    assert DM.apply(b"C:\\CAD\\XQZ3DR\\M02 Axis\\") == b"C:\\CAD\\WRT3LR\\M02 Axis\\"
    assert DM.apply(b"\\XQZ3DR v2 - TEST\\") == b"\\WRT3LR v2 - TEST\\"
    u = "C:\\CAD\\XQZ3DR\\".encode("utf-16-le")
    assert DM.apply(u) == "C:\\CAD\\WRT3LR\\".encode("utf-16-le")
    assert DM.apply(b"in xzorb_logo ") == b"in xzorb_logo "       # no hard start: untouched
    assert DM.apply(b" XQZ3DRS ") == b" XQZ3DRS "                 # two glued letters: a different word
    assert DM.apply(b" Xqz3dr ") == b" Xqz3dr "                   # not an upper-case product code
    assert DM.apply(b" QUORVEXR ") == b" QUORVEXR "               # model-letter rule needs a key with a digit


def test_short_name_digits_then_terminator():
    assert DM.apply(b"\xe1\xe3\xe2\x01ZORB3\x00\xe1") == b"\xe1\xe3\xe2\x01QAXE3\x00\xe1"
    assert DM.apply(b"\x9a\x12zorb3\x88") == b"\x9a\x12zorb3\x88"        # high byte after digits: still noise


def test_reference_stems_follow_renamed_keys():
    from deid.rewrite.mapping import ref_stems
    keys = [("sources/x/ZORB3_006_A_001_HEAD.asm.1", DM.key("sources/x/ZORB3_006_A_001_HEAD.asm.1")),
            ("sources/x/Quorvex.prt", DM.key("sources/x/Quorvex.prt"))]
    refs = ref_stems(keys)
    assert refs == {"ZORB3_006_A_001_HEAD": "QAXE3_006_A_001_HEAD"}       # 'Quorvex': not distinctive
    dm = DeidMap([dict(surface=k, pseudonym=v, scope=s, widths_ok=True)
                  for s, m in DM.maps.items() for k, v in m.items()], refs)
    rec = b"\xc0\xb2kZORB3_006_A_001_HEAD.ASM\x00"   # record byte that happens to be a letter
    assert DM.apply(rec) == rec                         # word rules alone cannot see it
    assert dm.apply(rec) == b"\xc0\xb2kQAXE3_006_A_001_HEAD.ASM\x00"


def test_short_name_part_number_after_record_byte():
    assert DM.apply(b"\xc0\xb2<ZORB3_006_A_001_HEAD.ASM\x00") == b"\xc0\xb2<QAXE3_006_A_001_HEAD.ASM\x00"
    assert DM.apply(b" xZORB3_006 ") == b" xZORB3_006 "      # letter before: inside a word


def test_pdf_embedded_file_content_and_name_rewritten(tmp_path):
    import pymupdf
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.pdf import rewrite_pdf
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "assembly notes")
    doc.embfile_add("Quorvex_notes.txt", b"drawn by Mira Talvenk\n", filename="Quorvex_notes.txt")
    doc.save(src)
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={}, tmp=tmp_path))
    assert o.status == "ok", o.info
    with pymupdf.open(dst) as d:
        name = d.embfile_names()[0]
        assert d.embfile_get(name) == b"drawn by Ines Fintaro\n"
        assert d.embfile_info(name)["filename"] == "Bandulo_notes.txt"


def _jpeg_pdf(path):
    import pymupdf
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(im).rectangle((250, 200, 380, 280), fill=(200, 30, 30))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=95)
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_image(pymupdf.Rect(50, 50, 450, 350), stream=buf.getvalue())
    doc.save(path)
    with pymupdf.open(path) as d:
        xref = d[0].get_images(full=True)[0][0]
        assert d[0].get_images(full=True)[0][8] == "DCTDecode"
        return __import__("hashlib").sha256(pymupdf.Pixmap(d, xref).tobytes("png")).hexdigest()


def test_pdf_jpeg_logo_uses_pixmap_png_cache_key(tmp_path):
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.pdf import rewrite_pdf
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    key = _jpeg_pdf(src)
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={key: [((250, 200, 380, 280), 0.5)]}, tmp=tmp_path))
    assert o.status == "ok" and o.info["logos"] == 1 and o.info["logo_cache_miss"] == 0


def test_pdf_logo_cache_miss_detects_live(tmp_path, monkeypatch):
    from deid.rewrite import image as I
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.pdf import rewrite_pdf
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _jpeg_pdf(src)
    monkeypatch.setattr(I, "detect_logos", lambda work: [((250, 200, 380, 280), 0.5)])
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={"someotherimage": []}, tmp=tmp_path))
    assert o.info["logo_cache_miss"] == 1 and o.info["logos"] == 1


def test_whole_image_logo_and_signature_blanked(tmp_path):
    import hashlib
    from PIL import Image, ImageDraw
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.image import scrub_bytes
    im = Image.new("RGB", (200, 80), "white")
    ImageDraw.Draw(im).text((10, 30), "QUORVEX", fill=(20, 120, 40))
    buf = io.BytesIO(); im.save(buf, "PNG"); data = buf.getvalue()
    h = hashlib.sha256(data).hexdigest()
    for kind in ("logo", "signature"):
        new, info = scrub_bytes(data, Ctx(DM, logo_cache={h: []}, blank_images={h: kind}, tmp=tmp_path))
        out = Image.open(io.BytesIO(new)).convert("RGB")
        assert new != data and out.size == im.size
        assert info.get("logos" if kind == "logo" else "signatures") == 1
        assert out.getpixel((40, 38)) != (20, 120, 40)          # the mark is gone
        if kind == "signature":
            assert len(set(out.getdata())) == 1                # flat blank
    plain = Image.new("RGB", (200, 80), "white")
    ImageDraw.Draw(plain).text((10, 30), "BRACKET", fill=(20, 120, 40))
    buf = io.BytesIO(); plain.save(buf, "PNG"); pdata = buf.getvalue()
    ph = hashlib.sha256(pdata).hexdigest()
    new, info = scrub_bytes(pdata, Ctx(DM, logo_cache={ph: []}, blank_images={h: "logo"}, tmp=tmp_path))
    assert new == pdata                                          # not in the set, nothing mapped: untouched


def test_pdf_whole_image_blank_uses_pixmap_png_key(tmp_path):
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.pdf import rewrite_pdf
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    key = _jpeg_pdf(src)
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={key: []}, blank_images={key: "logo"}, tmp=tmp_path))
    assert o.status == "ok" and o.info["logos"] == 1 and o.info["images_replaced"] == 1


def test_scrubbed_grey_and_bilevel_images_keep_their_mode(tmp_path):
    import hashlib
    from PIL import Image, ImageDraw
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.image import scrub_bytes
    for mode in ("L", "1"):
        im = Image.new(mode, (200, 80), 255)
        ImageDraw.Draw(im).rectangle((20, 20, 120, 60), fill=0)
        buf = io.BytesIO(); im.save(buf, "PNG"); data = buf.getvalue()
        h = hashlib.sha256(data).hexdigest()
        new, info = scrub_bytes(data, Ctx(DM, logo_cache={h: []}, blank_images={h: "logo"}, tmp=tmp_path))
        assert info["logos"] == 1 and Image.open(io.BytesIO(new)).mode == mode


def test_pdf_replacement_does_not_erase_neighbouring_lines(tmp_path):
    import pymupdf
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.pdf import rewrite_pdf
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_text((72, 100), "BRACKET INSTITUTE OF TECHNOLOGY", fontsize=10)
    page.insert_text((72, 108), "QUORVEX PARTSVAULT INSTITUTE OF MECHANICS", fontsize=10)   # pitch < char bbox height
    page.insert_text((72, 116), "SPROCKET UNIVERSITY GEAR GROUP", fontsize=10)
    doc.save(src)
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={}, tmp=tmp_path))
    with pymupdf.open(dst) as d:
        txt = d[0].get_text("text")
    assert o.info["text_replaced"] >= 1 and "QUORVEX" not in txt.upper()
    assert "BRACKET INSTITUTE OF TECHNOLOGY" in txt and "SPROCKET UNIVERSITY GEAR GROUP" in txt
    assert "INSTITUTE OF MECHANICS" in txt


def _inline_image_pdf(path):
    """A page with an INLINE image (BI ... ID ... EI, no xref) next to a text line."""
    import pymupdf
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_text((150, 120), "BRACKET INSTITUTE OF TECHNOLOGY", fontsize=10)
    data = bytes([0, 255, 255, 0] * 4)
    xref = page.get_contents()[0]
    stream = doc.xref_stream(xref) + b"\nq 40 0 0 30 100 100 cm BI /W 4 /H 4 /CS /G /BPC 8 ID " + data + b" EI Q\n"
    doc.update_stream(xref, stream)
    doc.save(path)


def test_inline_image_region_is_blanked_and_neighbour_text_kept(tmp_path):
    import pymupdf
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.pdf import rewrite_pdf
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _inline_image_pdf(src)
    with pymupdf.open(src) as d:
        infos = d[0].get_image_info(xrefs=True)
        assert [i["xref"] for i in infos] == [0] and d[0].get_images() == []     # invisible to the xref image pass
        rect = list(infos[0]["bbox"])
    regs = {"blob": [dict(page=0, rect=rect, kind="logo", propagate=False)]}
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={}, vector_logos=regs, blob_sha="blob", tmp=tmp_path))
    assert o.info["region_logos"] == 1
    with pymupdf.open(dst) as d:
        assert "BRACKET INSTITUTE OF TECHNOLOGY" in d[0].get_text("text")
        pix = d[0].get_pixmap(clip=pymupdf.Rect(rect), dpi=72)
        assert min(pix.samples) > 100                                         # the black image pixels are gone
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={}, vector_logos=regs, blob_sha="other", tmp=tmp_path))
    assert "region_logos" not in o.info                                       # regions belong to their own blob


def test_stage4_boundary_rules():
    # typographic delimiters around short keys
    assert DM.apply("the ZORB’s arm".encode()) == "the QAXE’s arm".encode()
    assert DM.apply("ZORB® GmbH".encode()) == "QAXE® GmbH".encode()
    assert DM.apply("“ZORB”".encode()) == "“QAXE”".encode()
    assert DM.apply(b"\x9a\x12zorb\xae\x88") == b"\x9a\x12zorb\xae\x88"              # binary noise stays
    # '<' glued to a preceding letter/digit is a CAD record, not markup; real tags stay protected
    assert DM.apply(b"FAN_8X8<QUORVEX_FAN-0710>.PRT") == b"FAN_8X8<BANDULO_FAN-0710>.PRT"
    assert DM.apply(b"<Quorvex attr='1'>") == b"<Quorvex attr='1'>"
    # mixed letter+digit keys (>= 5 chars) need one hard edge
    assert DM.apply(b"D:\\XQZ3Dlabs\\x") == b"D:\\WRT3Llabs\\x"
    assert DM.apply(b"A3XQZ3DPaste-M01") == b"A3XQZ3DPaste-M01"                    # still soft on both sides


def test_page_scan_renders_inline_images_in_bounded_batches(monkeypatch):
    """Stage 4: a page with many inline images must not render every crop before classifying (OOM-killed workers)."""
    import pymupdf
    import deid.logo_clip as lc
    import deid.vector_logo_scan as vs
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_text((30, 60), "TITLE", fontsize=10)
    data = bytes([0, 255, 255, 0] * 4)
    xref = page.get_contents()[0]
    imgs = b"".join(b"q 20 0 0 15 %d %d cm BI /W 4 /H 4 /CS /G /BPC 8 ID " % (30 + 25 * (i % 20), 100 + 20 * (i // 20))
                    + data + b" EI Q\n" for i in range(75))
    doc.update_stream(xref, doc.xref_stream(xref) + b"\n" + imgs)
    alive, peak = [0], [0]
    real_render = vs._render

    def render(pg, clip=None):
        alive[0] += 1; peak[0] = max(peak[0], alive[0])
        return real_render(pg, clip)

    def classify(ims):
        alive[0] -= len(ims)
        return [dict(logo=0.95, signature=0.0) for _ in ims]

    monkeypatch.setattr(vs, "_render", render)
    monkeypatch.setattr(lc, "classify", classify)
    out = vs.scan_page(doc[0], vector=False)
    assert len(out) == 75 and all(c["src"] == "inline" for c in out)
    assert peak[0] <= vs.INLINE_BATCH


def test_page_scan_records_jobs_of_dead_workers(tmp_path, monkeypatch):
    import json
    import deid.vector_logo_scan as vs
    monkeypatch.setattr(vs, "INFLIGHT", tmp_path)
    (tmp_path / "999999999").write_text(json.dumps(["abc", "sources/x.pdf"]))
    (tmp_path / str(__import__("os").getpid())).write_text(json.dumps(["def", "sources/y.pdf"]))  # alive: kept
    dead = vs._died()
    assert [d["sha256"] for d in dead] == ["abc"] and dead[0]["error"] == vs.DIED
    assert (tmp_path / str(__import__("os").getpid())).exists()


def test_short_field_seed_needs_a_real_field_not_float_bytes():
    """Stage 4: a 3-letter author seed matched '<qeB=' / '<QEb;' inside packed geometry (SolidWorks verifier)."""
    dm = _dm(**{"field": {"Qeb": "Vom"}})
    for noise in (b"+d\x0f<qeB=\x9b\xee", b"\x84\xc3&\xbc&\xe4\x83<QEb;H\xdd", b"\x9b<QEB;\x9b", b"\x01=Qeb,\x02"):
        assert dm.apply(noise) == noise
    assert dm.apply(b'Author="Qeb"') == b'Author="Vom"'
    assert dm.apply(b"<Author>Qeb</Author>") == b"<Author>Vom</Author>"
    assert dm.apply(b"/Author (Qeb)") == b"/Author (Vom)"
    assert dm.apply(b"\x00\x00\x00\x04\x00\x00\x00Qeb\x00") == b"\x00\x00\x00\x04\x00\x00\x00Vom\x00"   # VT_LPSTR
    assert dm.apply("'Qeb'".encode("utf-16-le")) == "'Vom'".encode("utf-16-le")
    assert dm.apply(b"Author;Qeb;2024-01-02") == b"Author;Vom;2024-01-02"     # loose delimiters in plain text: kept
    assert dm.apply(b"Author;qEB;2024") == b"Author;qEB;2024"                 # token casing required
    mfc = b"\xff\xfe\xff\x03" + "Qeb".encode("utf-16-le")
    assert dm.apply(b"Array\x01\x00" + mfc + b"\x05\x80\x01\x00") == b"Array\x01\x00\xff\xfe\xff\x03" + "Vom".encode("utf-16-le") + b"\x05\x80\x01\x00"
    assert dm.apply(b"\x03\x00\x00\x00" + "Qeb".encode("utf-16-le") + b"\x80\x0c") == b"\x03\x00\x00\x00" + "Vom".encode("utf-16-le") + b"\x80\x0c"
    assert dm.apply(b"FORMAT=x;\nUSER=Qeb\nMODE=b") == b"FORMAT=x;\nUSER=Vom\nMODE=b"


def test_page_scan_render_is_capped_on_huge_pages(monkeypatch):
    import pymupdf
    import deid.vector_logo_scan as vs
    doc = pymupdf.open(); page = doc.new_page(width=30000, height=30000)  # ~417 in square: 1.7 GP at 100 dpi
    im = vs._render(page)
    assert im.width * im.height <= vs.MAX_PIXELS
    strip = doc.new_page(width=20000, height=300)                         # elongated: OWLv2 input side capped
    seen = []
    monkeypatch.setattr("deid.rewrite.image.detect_logos", lambda im: seen.append(im.size) or [])
    vs.scan_page(strip, vector=True)
    assert seen and max(seen[0]) <= vs.MAX_SIDE


def test_region_blank_removes_stamp_annotation_logo(tmp_path):
    """Stage 4: a logo stored as a Stamp annotation is drawn on top of the page: the region blank must remove it."""
    import pymupdf
    from deid.rewrite.dispatch import Ctx
    from deid.rewrite.pdf import rewrite_pdf
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_text((150, 300), "BRACKET INSTITUTE OF TECHNOLOGY", fontsize=10)
    stamp = page.add_stamp_annot(pymupdf.Rect(100, 100, 220, 140), stamp=0)
    stamp.set_colors(stroke=(1, 0, 0)); stamp.update()
    page.add_text_annot((400, 400), "review note")                 # elsewhere on the page: kept
    doc.save(src)
    regs = {"blob": [dict(page=0, rect=[100, 100, 220, 140], kind="logo", propagate=False)]}
    o = rewrite_pdf(src, dst, Ctx(DM, logo_cache={}, vector_logos=regs, blob_sha="blob", tmp=tmp_path))
    assert o.info["region_annots_removed"] == 1
    with pymupdf.open(dst) as d:
        assert [a.type[1] for a in d[0].annots()] == ["Text"]
        s = d[0].get_pixmap(clip=pymupdf.Rect(100, 100, 220, 140), dpi=72).samples
        assert not any(s[i] > 150 and s[i + 1] < 90 for i in range(0, len(s) - 2, 3))   # no red stamp left
