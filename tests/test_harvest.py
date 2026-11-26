"""Stage 1 harvester unit tests (synthetic fixtures)."""
from deid.harvest import Harvest, harvest_bytes, step_decode, text_runs, plausible


def kinds(h, prefix):
    return {v for (k, v) in h.strings if k.startswith(prefix)}


def test_step_header_fields_and_escapes():
    step = (b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('desc'),'2;1');\n"
            b"FILE_NAME('part.stp','2020-01-01T00:00:00',('Jane Doe'),('ACME Corp'),'pp','SolidWorks','');\n"
            b"FILE_SCHEMA(('AP214'));\nENDSEC;\nDATA;\n#1=PRODUCT('Widget \\X2\\767D9E64\\X0\\','x','',(#2));\n"
            b"#2=CARTESIAN_POINT('',(0.,1.,2.));\nENDSEC;\nEND-ISO-10303-21;\n")
    h = harvest_bytes(step, "step")
    assert kinds(h, "step.file_name.author") == {"Jane Doe"}
    assert kinds(h, "step.file_name.org") == {"ACME Corp"}
    assert "Widget 白鹤" in kinds(h, "step.data")
    assert step_decode(b"O''Brien") == "O'Brien"


def test_step_gbk_header():
    raw = "张三".encode("gbk")
    h = harvest_bytes(b"ISO-10303-21;\nHEADER;\nFILE_NAME('a','t',('" + raw + b"'),(''),'','','');\nENDSEC;\nDATA;\n", "step")
    assert kinds(h, "step.file_name.author") == {"张三"}


def test_iges_global_author_org():
    g = "1H,,1H;,4Hpart,8Hpart.igs,9HSolidEdge,3H1.0,32,38,6,308,15,4Hpart,1.,2,2HMM,1,1.,15H20200101.000000,0.001,100.,8HJane Doe,8HACME Inc,11,0,15H20200101.000000;"
    lines = [g[i:i + 72].ljust(72) + "G" + str(n + 1).rjust(7) for n, i in enumerate(range(0, len(g), 72))]
    data = ("start comment by Bob".ljust(72) + "S      1\n" + "\n".join(lines) + "\n").encode()
    h = harvest_bytes(data, "iges")
    assert kinds(h, "iges.global.author") == {"Jane Doe"}
    assert kinds(h, "iges.global.org") == {"ACME Inc"}
    assert "start comment by Bob" in kinds(h, "iges.start")


def test_text_runs_utf16_and_cjk():
    h = Harvest()
    buf = (b"\x0e\x00" + "Century Gothic".encode("utf-16-le") + b"\x05\x01" + "江詠筑作业".encode("utf-16-le")
           + b"\x00\x00\x9aC:\\Users\\quintor\\x\x00" + bytes(range(256)) * 4)
    text_runs(h, buf, "r")
    vals = {v for (_k, v) in h.strings}
    assert {"Century Gothic", "江詠筑作业", "C:\\Users\\quintor\\x"} <= vals


def test_plausible_rejects_noise():
    assert not plausible("x9$#")
    assert plausible("OTTAVIO QUARTAVELLI")


def test_json_and_markup():
    h = harvest_bytes(b'{"author": "Jane \\u00c9", "pts": [1,2,3]}', "unknown:json")
    assert "Jane É" in kinds(h, "json.")
    svg = b'<svg><metadata><dc:creator><rdf:Bag><rdf:li>Bob Smith</rdf:li></rdf:Bag></dc:creator></metadata><path d="M 0 0"/><text>Logo ACME</text></svg>'
    h = harvest_bytes(svg, "svg")
    assert "Bob Smith" in kinds(h, "svg.prop.creator") and "Logo ACME" in kinds(h, "svg.text")
    assert not any(k == "svg.attr.d" for (k, _v) in h.strings)


def test_obj_stl_dxf():
    h = harvest_bytes(b"# made by Jane\no Bracket_ACME\nv 1 2 3\nusemtl steel\nf 1 2 3\n", "obj")
    assert {"made by Jane", "Bracket_ACME", "steel"} <= {v for (_k, v) in h.strings}
    h = harvest_bytes(b"solid ACME part\nfacet normal 0 0 1\nendsolid ACME part\n", "stl-ascii")
    assert kinds(h, "stl.name") == {"ACME part"}
    dxf = b"0\nSECTION\n2\nHEADER\n9\n$LASTSAVEDBY\n1\njdoe\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nTEXT\n8\nLayer1\n1\nDrawn by J. Doe\n10\n1.0\n0\nENDSEC\n0\nEOF\n"
    h = harvest_bytes(dxf, "dxf")
    assert kinds(h, "dxf.hdr.$LASTSAVEDBY") == {"jdoe"} and "Drawn by J. Doe" in kinds(h, "dxf.c1")
