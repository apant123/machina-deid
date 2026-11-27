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
