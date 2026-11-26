"""Stage 1: per-format string harvest.

`harvest_file(path, fmt)` returns a `Harvest`: a Counter of (kind, value) plus an
inventory of embedded images (for the OCR / logo pass) and notes. Harvesters only
*read*; nothing is decided here about what is identifying (that is Stage 2).

Design rules
* Geometry is never harvested: numbers are skipped, only string-typed fields,
  comments, names, metadata and text runs are kept.
* `kind` records *where* a string came from (e.g. `step.file_name.author`,
  `ole.meta.last_saved_by`, `pdf.text`, `sw.run`), so Stage 2 can weight field
  strings (high prior of being a person/org) above free text runs.
* Binary formats fall back to text runs (ASCII, UTF-16LE, UTF-8 CJK) filtered by a
  plausibility test, so random bytes in geometry rarely appear.
* Files are mmapped; nothing loads a multi-GB file into memory except formats that
  must be parsed as a whole (SolidWorks sections, OLE2, PDF, zip containers).
"""
from __future__ import annotations

import io
import mmap
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MAX_VALUE = 400          # chars kept per string
MAX_UNIQUE = 150_000     # unique (kind, value) per file before truncating


@dataclass
class Harvest:
    strings: Counter = field(default_factory=Counter)
    images: int = 0               # embedded raster images / thumbnails (OCR inventory)
    scanned_pages: int = 0        # PDF pages with images but (almost) no text layer
    notes: list = field(default_factory=list)
    truncated: bool = False

    def add(self, kind: str, value, n: int = 1) -> None:
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = decode_bytes(bytes(value))
        v = norm(value)
        if v is None:
            return
        if len(self.strings) >= MAX_UNIQUE and (kind, v) not in self.strings:
            self.truncated = True
            return
        self.strings[(kind, v)] += n


# --------------------------------------------------------------------------- helpers
_WS = re.compile(r"\s+")


def norm(s: str) -> str | None:
    s = _WS.sub(" ", s.replace("\x00", " ")).strip()
    if len(s) < 2 or not any(c.isalpha() for c in s):
        return None
    return s[:MAX_VALUE]


def decode_bytes(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if any(c >= 0x81 for c in b):
        try:
            s = b.decode("gbk")  # Chinese ANSI (STEP headers, old CAD)
            if sum("\u4e00" <= c <= "\u9fff" for c in s) >= 1:
                return s
        except UnicodeDecodeError:
            pass
    return b.decode("cp1252", "replace")


def _is_cjk(c: str) -> bool:
    return "\u3040" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7af" or "\uf900" <= c <= "\ufaff"


_OKPUNCT = set(" _-./\\:@()&,'+#~[]")


def plausible(s: str) -> bool:
    """Filter for text runs pulled out of binary data."""
    if not s:
        return False
    if any(_is_cjk(c) for c in s):
        return True
    letters = sum(c.isalpha() for c in s)
    if letters < 3:
        return False
    ok = sum(c.isalnum() or c in _OKPUNCT for c in s)
    return ok / len(s) >= 0.85 and letters / len(s) >= 0.4


_RUN8 = re.compile(rb"[\x20-\x7e]{4,}")
_RUN16 = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
# UTF-16LE CJK: must start with two ideograph code units; parity is checked by decoding both offsets
_RUN16CJK = re.compile(rb"(?:[\x00-\xff][\x4e-\x9f]){2,}(?:[\x20-\x7e]\x00|[\x00-\xff][\x4e-\x9f])*")
_STR16CJK = re.compile(r"[\u0020-\u007e\u3040-\u30ff\u4e00-\u9fff，。、（）：；－]*[\u4e00-\u9fff]{2,}[\u0020-\u007e\u3040-\u30ff\u4e00-\u9fff，。、（）：；－]*")
_RUNCJK = re.compile(rb"(?:[\x20-\x7e]|[\xe4-\xe9][\x80-\xbf]{2}|\xe3[\x80-\x83][\x80-\xbf]){2,}")
_HAS_UTF8_CJK = re.compile(rb"(?:[\xe4-\xe9][\x80-\xbf]{2}){2}")


def _common_cjk(s: str) -> bool:
    """CJK decoded from binary is mostly noise unless the ideographs are common ones
    (GB2312 level 1, ~3,755 chars) or kana. ASCII misread as UTF-16 fails this."""
    cjk = [c for c in s if _is_cjk(c)]
    if len(cjk) < 2:
        return False
    good = 0
    for c in cjk:
        if "぀" <= c <= "ヿ":
            good += 1
            continue
        try:
            b = c.encode("gb2312")
            good += 0xB0 <= b[0] <= 0xD7
        except UnicodeEncodeError:
            pass
    return good / len(cjk) >= 0.8


def _cjk_ok(s: str) -> bool:
    return "�" not in s and all(c.isascii() or _is_cjk(c) or c in "，。、（）：；－" for c in s) and _common_cjk(s)


def text_runs(h: Harvest, buf, kind: str, utf16: bool = True) -> None:
    local = Counter(m.group() for m in _RUN8.finditer(buf))
    for raw, n in local.items():
        s = raw.decode("ascii")
        if plausible(s):
            h.add(kind, s, n)
    if utf16:
        local = Counter(m.group() for m in _RUN16.finditer(buf))
        for raw, n in local.items():
            s = raw.decode("utf-16-le")
            if plausible(s):
                h.add(kind + "16", s, n)
        local = Counter()
        last = -1
        for m in _RUN16CJK.finditer(buf):
            if m.start() < last:
                continue
            a, b = max(0, m.start() - 2), min(len(buf), m.end() + 80)
            w = bytes(buf[a:b])
            for off in (0, 1):  # the byte regex can lock onto the wrong parity: decode both
                txt = w[off:len(w) - ((len(w) - off) % 2)].decode("utf-16-le", "replace")
                for t in _STR16CJK.findall(txt):
                    if _cjk_ok(t):
                        local[t] += 1
            last = b
        for t, n in local.items():
            h.add(kind + "16", t, n)
    local = Counter(m.group() for m in _RUNCJK.finditer(buf) if _HAS_UTF8_CJK.search(m.group()))
    for raw, n in local.items():
        s = raw.decode("utf-8", "replace")
        if _cjk_ok(s):
            h.add(kind + "_utf8", s, n)


def text_lines(h: Harvest, buf, kind: str, min_letters: int = 3) -> None:
    """Generic text: every line that contains letters (chunked), counted."""
    local = Counter()
    for m in re.finditer(rb"[^\r\n]+", buf):
        line = m.group()
        if sum(1 for c in line[:600] if 65 <= c <= 90 or 97 <= c <= 122 or c >= 0x80) < min_letters:
            continue
        for i in range(0, min(len(line), 4000), MAX_VALUE):
            local[line[i:i + MAX_VALUE]] += 1
    for raw, n in local.items():
        h.add(kind, raw, n)


# ---- markup (XML/SVG/HTML/OOXML) --------------------------------------------------
_XML_TEXT = re.compile(rb">([^<]{2,})<")
_XML_ATTR = re.compile(rb"""\s([A-Za-z_][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_XML_COMMENT = re.compile(rb"<!--(.*?)-->", re.S)
_SKIP_ATTRS = {b"d", b"points", b"transform", b"style", b"viewBox", b"x", b"y", b"x1", b"y1", b"x2", b"y2",
               b"cx", b"cy", b"r", b"rx", b"ry", b"width", b"height", b"fill", b"stroke", b"stroke-width",
               b"offset", b"stop-color", b"gradientTransform", b"xmlns", b"version", b"encoding", b"type",
               b"vertices", b"triangles", b"v1", b"v2", b"v3", b"z", b"p1", b"p2", b"p3", b"pid", b"color"}
# name/value property pairs (OOXML/SW custom props, FreeCAD properties, 3MF metadata)
_PROP_PATTERNS = [
    re.compile(rb'name="([^"]{1,80})"[^>]*>\s*<vt:(?:lpw?str|bstr)>([^<]*)<'),
    re.compile(rb'<Property name="(\w+)" type="App::Property(?:String|Link|File|Path)[^"]*"[^>]*>\s*<\w+ (?:value|file)="([^"]*)"'),
    re.compile(rb'<metadata name="([^"]{1,80})"[^>]*>([^<]*)<'),
    re.compile(rb'<(?:dc|cp|dcterms|xmp|pdf|photoshop|xmpRights):(\w+)[^>]*>\s*(?:<rdf:\w+>\s*<rdf:li[^>]*>)?([^<]+)<'),
]
_ENT = {b"&amp;": b"&", b"&lt;": b"<", b"&gt;": b">", b"&quot;": b'"', b"&apos;": b"'"}


def _unent(b: bytes) -> bytes:
    if b"&" not in b:
        return b
    for k, v in _ENT.items():
        b = b.replace(k, v)
    b = re.sub(rb"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)).encode("utf-8", "replace"), b)
    return re.sub(rb"&#(\d+);", lambda m: chr(int(m.group(1))).encode("utf-8", "replace") if int(m.group(1)) < 0x110000 else b"", b)


def markup(h: Harvest, buf, kind: str) -> None:
    local = Counter()
    for m in _XML_TEXT.finditer(buf):
        local[(kind + ".text", m.group(1))] += 1
    for m in _XML_ATTR.finditer(buf):
        name, val = m.group(1), m.group(2) if m.group(2) is not None else m.group(3)
        if name in _SKIP_ATTRS or not val or val.startswith(b"data:") or len(val) > 4000:
            continue
        local[(kind + ".attr." + name.decode("latin1")[:40], val)] += 1
    for m in _XML_COMMENT.finditer(buf):
        local[(kind + ".comment", m.group(1)[:2000])] += 1
    for p in _PROP_PATTERNS:
        for m in p.finditer(buf):
            local[(kind + ".prop." + m.group(1).decode("latin1", "replace")[:60], m.group(2))] += 1
    for (k, raw), n in local.items():
        h.add(k, _unent(raw).strip(), n)
    h.images += len(re.findall(rb"data:image/", buf))


# --------------------------------------------------------------------------- formats
_STEP_STR = re.compile(rb"'((?:[^'\r\n]|'')+)'")
_STEP_X2 = re.compile(rb"\\X2\\((?:[0-9A-Fa-f]{4})+)\\X0\\")
_STEP_X4 = re.compile(rb"\\X4\\((?:[0-9A-Fa-f]{8})+)\\X0\\")
_STEP_X = re.compile(rb"\\X\\([0-9A-Fa-f]{2})")
_STEP_S = re.compile(rb"\\S\\(.)", re.S)


def step_decode(raw: bytes) -> str:
    b = raw.replace(b"''", b"'")
    if b"\\" in b:
        b = _STEP_X2.sub(lambda m: bytes.fromhex(m.group(1).decode()).decode("utf-16-be", "replace").encode(), b)
        b = _STEP_X4.sub(lambda m: bytes.fromhex(m.group(1).decode()).decode("utf-32-be", "replace").encode(), b)
        b = _STEP_X.sub(lambda m: bytes([int(m.group(1), 16)]).decode("latin1").encode(), b)
        b = _STEP_S.sub(lambda m: bytes([m.group(1)[0] + 128]).decode("latin1").encode(), b)
    return decode_bytes(b)


def h_step(h: Harvest, buf) -> None:
    end = buf.find(b"DATA;")
    end = end if end > 0 else min(len(buf), 65536)
    header = bytes(buf[:end])
    for m in re.finditer(rb"(FILE_NAME|FILE_DESCRIPTION|FILE_SCHEMA)\s*\((.*?)\)\s*;", header, re.S):
        strs = [step_decode(s) for s in _STEP_STR.findall(m.group(2))]
        if m.group(1) == b"FILE_NAME":
            # name, time_stamp, (author...), (organization...), preprocessor, originating_system, authorization
            groups = re.findall(rb"\(([^()]*)\)", m.group(2))
            authors = [step_decode(s) for s in _STEP_STR.findall(groups[0])] if len(groups) > 0 else []
            orgs = [step_decode(s) for s in _STEP_STR.findall(groups[1])] if len(groups) > 1 else []
            if strs:
                h.add("step.file_name.name", strs[0])
            for a in authors:
                h.add("step.file_name.author", a)
            for o in orgs:
                h.add("step.file_name.org", o)
            rest = [s for s in strs[1:] if s not in authors and s not in orgs]
            for s in rest[1:]:
                h.add("step.file_name.system", s)
        else:
            for s in strs:
                h.add("step." + m.group(1).decode().lower(), s)
    local = Counter(m.group(1) for m in _STEP_STR.finditer(buf, end))
    for raw, n in local.items():
        h.add("step.data", step_decode(raw), n)


IGES_GLOBAL = {3: "product_sender", 4: "file_name", 5: "system", 6: "preprocessor", 12: "product_receiver",
               15: "unit", 18: "date", 21: "author", 22: "org", 25: "date_modified"}


def _hollerith(text: str) -> list[tuple[int, str]]:
    """Parse IGES global section into positional params (strings only)."""
    out, i, idx, pd, rd = [], 0, 1, ",", ";"
    if text[:2] == "1H" and len(text) > 2:
        pd = text[2]
    params, cur, n = [], "", len(text)
    while i < n:
        m = re.match(r"\s*(\d+)H", text[i:])
        if m and (cur.strip() == ""):
            ln = int(m.group(1))
            s = text[i + m.end(): i + m.end() + ln]
            params.append(("s", s))
            i += m.end() + ln
            while i < n and text[i] not in (pd, rd):
                i += 1
            i += 1
            cur = ""
            continue
        c = text[i]
        if c in (pd, rd):
            params.append(("n", cur))
            cur = ""
        else:
            cur += c
        i += 1
    for k, (t, v) in enumerate(params, start=1):
        if t == "s":
            out.append((k, v))
        idx = k
    return out


def h_iges(h: Harvest, buf) -> None:
    sec = {"S": [], "G": [], "P": []}
    for m in re.finditer(rb"[^\r\n]+", buf):
        line = m.group()
        if len(line) >= 73:
            c = chr(line[72])
            if c in ("S", "G"):
                sec[c].append(decode_bytes(bytes(line[:72])))
            elif c == "P":
                sec["P"].append(decode_bytes(bytes(line[:64])))
            elif c == "T":
                break
    for s in sec["S"]:
        h.add("iges.start", s)
    for k, v in _hollerith("".join(sec["G"])):
        h.add("iges.global." + IGES_GLOBAL.get(k, str(k)), v)
    p = "".join(sec["P"])
    for m in re.finditer(r"(?:^|[,;])\s*(\d+)H", p):
        h.add("iges.param", p[m.end(): m.end() + int(m.group(1))])


def h_sat(h: Harvest, buf) -> None:
    head = bytes(buf[:4096])
    if head.startswith(b"ACIS BinaryFile"):
        text_runs(h, buf, "sat.run", utf16=False)
        return
    lines = head.split(b"\n")[:3]
    for ln in lines[1:3]:
        h.add("sat.header", ln)
    local = Counter()
    for m in re.finditer(rb"@(\d+) ", buf):
        n = int(m.group(1))
        if 0 < n < 2000:
            local[bytes(buf[m.end():m.end() + n])] += 1
    for raw, n in local.items():
        h.add("sat.string", raw, n)
    if not local:  # pre-v7 SAT: no @-prefixed strings; fall back to word tokens
        toks = Counter(re.findall(rb"(?<![\w$@-])[A-Za-z][A-Za-z0-9_ .-]{3,60}(?=\s[#$\d-])", buf))
        for raw, n in toks.items():
            h.add("sat.token", raw, n)


def h_parasolid_text(h: Harvest, buf) -> None:
    end = buf.find(b"**END_OF_HEADER")
    head = bytes(buf[: end if end > 0 else 4096])
    for part in re.split(rb"[;\r\n]", head):
        if b"=" in part:
            k, _, v = part.partition(b"=")
            h.add("xt.header." + k.strip(b"* ").decode("latin1")[:30], v)
        else:
            h.add("xt.header", part.strip(b"* "))
    toks = Counter(re.findall(rb"[A-Za-z][A-Za-z0-9_.-]{3,}", buf, ) if len(buf) < 50_000_000 else [])
    for raw, n in toks.items():
        h.add("xt.token", raw, n)


def h_stl_ascii(h: Harvest, buf) -> None:
    for m in re.finditer(rb"^[ \t]*(?:end)?solid[ \t]*([^\r\n]*)", buf, re.M):
        h.add("stl.name", m.group(1))


def h_stl_binary(h: Harvest, buf) -> None:
    h.add("stl.header", bytes(buf[:80]).split(b"\x00")[0])
    hdr = bytes(buf[:80]).decode("latin1")
    for run in re.findall(r"[\x20-\x7e]{3,}", hdr):
        if plausible(run):
            h.add("stl.header", run)


_OBJ = re.compile(rb"^[ \t]*(#|o|g|usemtl|mtllib|newmtl|map_\w+)[ \t]+([^\r\n]*)", re.M)
_OBJ_KIND = {b"#": "comment", b"o": "object", b"g": "group", b"usemtl": "material", b"mtllib": "mtllib",
             b"newmtl": "material"}


def h_obj(h: Harvest, buf) -> None:
    local = Counter((m.group(1), m.group(2)) for m in _OBJ.finditer(buf))
    for (t, v), n in local.items():
        h.add("obj." + _OBJ_KIND.get(t, "map"), v, n)


def h_off(h: Harvest, buf) -> None:
    for m in re.finditer(rb"#([^\r\n]*)", buf):
        h.add("off.comment", m.group(1))


def h_vrml(h: Harvest, buf) -> None:
    local = Counter()
    for m in re.finditer(rb"#([^\r\n]*)", buf):
        local[("vrml.comment", m.group(1))] += 1
    for m in re.finditer(rb"\b(?:DEF|USE)\s+([^\s{}\[\]]+)", buf):
        local[("vrml.def", m.group(1))] += 1
    for m in re.finditer(rb'"([^"\r\n]{1,1000})"', buf):
        local[("vrml.string", m.group(1))] += 1
    for (k, v), n in local.items():
        h.add(k, v, n)


_DXF_TEXT_CODES = {1, 2, 3, 4, 6, 7, 8, 300, 301, 302, 303, 304, 305, 306, 307, 308, 309, 999,
                   1000, 1001, 1003}


def h_dxf(h: Harvest, buf) -> None:
    head = bytes(buf[:22])
    if head.startswith(b"AutoCAD Binary DXF"):
        text_runs(h, buf, "dxf.run", utf16=False)
        return
    lines = re.split(rb"\r?\n", bytes(buf))
    local = Counter()
    hdrvar = None
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i].strip() or b"-1")
        except ValueError:
            local[("dxf.unparsed", lines[i])] += 1
            continue
        val = lines[i + 1]
        if code == 9:
            hdrvar = val.strip().decode("latin1")
            continue
        if code in _DXF_TEXT_CODES or (hdrvar and code == 1):
            k = f"dxf.hdr.{hdrvar}" if hdrvar else f"dxf.c{code}"
            local[(k, val)] += 1
        if code == 0:
            hdrvar = None
    for (k, v), n in local.items():
        s = decode_bytes(v)
        # MTEXT inline formatting: \P paragraph, {\fArial|b0;...}
        s = re.sub(r"\\[A-Za-z][^;\\]*;|\\P|[{}]", " ", s) if "\\" in s else s
        s = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)
        h.add(k, s, n)


def h_postscript(h: Harvest, buf) -> None:
    local = Counter()
    for m in re.finditer(rb"^%%(\w+):[ \t]*([^\r\n]*)", buf, re.M):
        local[("ps.dsc." + m.group(1).decode("latin1")[:30], m.group(2))] += 1
    for m in re.finditer(rb"^%(?!%)([^\r\n]*)", buf, re.M):
        local[("ps.comment", m.group(1))] += 1
    for m in re.finditer(rb"\(((?:[^()\\\r\n]|\\.){2,400})\)", buf):
        local[("ps.string", m.group(1))] += 1
    for (k, v), n in local.items():
        h.add(k, v, n)
    h.images += len(re.findall(rb"\b(?:image|colorimage|imagemask)\b", buf[:5_000_000]))


def h_creo(h: Harvest, buf) -> None:
    first = bytes(buf[:200]).split(b"\n")[0]
    h.add("creo.ugc", first)
    for k in (rb"HOST", rb"DBID", rb"@model_name", rb"@author", rb"USER"):
        for m in re.finditer(re.escape(k) + rb"[\s=:]+([^\s\x00]{2,80})", buf[:2_000_000]):
            h.add("creo." + k.decode().lstrip("@").lower(), m.group(1))
    text_runs(h, buf, "creo.run", utf16=False)


# ---- SolidWorks 2015+ container ------------------------------------------------
SW_MARK = b"\x14\x00\x06\x00\x08\x00"


def sw_sections(d: bytes):
    """(name, inflated data) for every valid compressed section (see docs/SPIKES.md)."""
    pos = d.find(SW_MARK)
    while pos != -1:
        hdr = pos + 6
        if hdr + 20 <= len(d):
            _x, crc, b, c, nlen = struct.unpack_from("<IIIII", d, hdr)
            ds = hdr + 20 + nlen
            if 0 < b <= len(d) - ds and nlen < 400:
                try:
                    do = zlib.decompressobj(-15)
                    dec = do.decompress(d[ds:ds + b])
                    if do.eof and len(dec) == c and zlib.crc32(dec) == crc:
                        name = bytes(((x >> 4) | (x << 4)) & 0xFF for x in d[hdr + 20:ds]).decode("latin1")
                        yield name, dec
                        pos = d.find(SW_MARK, ds + b)
                        continue
                except zlib.error:
                    pass
        pos = d.find(SW_MARK, pos + 1)


def h_solidworks(h: Harvest, buf) -> None:
    d = bytes(buf)
    n = 0
    for name, data in sw_sections(d):
        n += 1
        h.add("sw.section", name)
        if "Preview" in name or name.lower().endswith((".png", ".bmp", ".jpg")) or data[:4] == b"\x89PNG":
            h.images += 1
            continue
        if b"<?xml" in data[:200] or b"<vt:" in data or b"<cp:" in data:
            markup(h, data, "sw.xml")
        text_runs(h, data, "sw.run")
    if n == 0:
        h.notes.append("no sw sections parsed; raw text runs")
        text_runs(h, buf, "sw.raw")


# ---- OLE2 (old SolidWorks, Inventor, Solid Edge, Altium, ...) ----------------------
_OLE_META = ("author", "last_saved_by", "company", "manager", "title", "subject", "keywords", "comments",
             "template", "category", "creating_application")


def _zlb(data: bytes) -> bytes | None:
    for off in range(0, min(64, len(data) - 2)):
        if data[off] == 0x78 and data[off + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            try:
                return zlib.decompressobj().decompress(data[off:], 200_000_000)
            except zlib.error:
                continue
    return None


def h_ole(h: Harvest, path: str) -> None:
    import olefile
    with olefile.OleFileIO(path) as ole:
        try:
            meta = ole.get_metadata()
            for f in _OLE_META:
                v = getattr(meta, f, None)
                if isinstance(v, bytes):
                    h.add("ole.meta." + f, v.rstrip(b"\x00"))
                elif isinstance(v, str):
                    h.add("ole.meta." + f, v)
        except Exception as e:  # noqa: BLE001
            h.notes.append(f"ole meta: {e!r}"[:200])
        for s in ole.listdir(streams=True, storages=True):
            for part in s:
                h.add("ole.entry", part)
        for s in ole.listdir(streams=True, storages=False):
            name = "/".join(s)
            try:
                data = ole.openstream(s).read()
            except Exception as e:  # noqa: BLE001
                h.notes.append(f"stream {name}: {e!r}"[:200])
                continue
            low = name.lower()
            if "preview" in low or "thumbnail" in low or data[:4] == b"\x89PNG":
                h.images += 1
                continue
            if low.endswith("__zlb"):
                inner = _zlb(data)
                if inner is not None:
                    data = inner
            if b"<?xml" in data[:300]:
                markup(h, data, "ole.xml")
            text_runs(h, data, "ole.run")


# ---- zip containers (FreeCAD, 3MF, Fusion f3d/f3z, ODG) ----------------------------
_IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
_TEXTY_EXT = (".xml", ".rels", ".model", ".json", ".txt", ".config", ".svg", ".html", ".csv", ".ini")


def h_zipcontainer(h: Harvest, path: str, fmt: str) -> None:
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            h.add(f"{fmt}.member", info.filename.rsplit("/", 1)[-1])
            low = info.filename.lower()
            if info.file_size > 500_000_000:
                h.notes.append(f"member too large: {info.filename}")
                continue
            try:
                data = z.read(info)
            except Exception as e:  # noqa: BLE001
                h.notes.append(f"member {info.filename}: {e!r}"[:200])
                continue
            if low.endswith(".model") and len(data) > 5_000_000:  # 3MF mesh: drop vertex/triangle lists
                data = re.sub(rb"<(vertices|triangles)>.*?</\1>", b"", data, flags=re.S)
            if low.endswith((".f3d", ".fcstd", ".3mf")) and data[:4] == b"PK\x03\x04":  # nested (f3z holds f3d)
                with tempfile.NamedTemporaryFile(suffix=".zip") as t:
                    t.write(data)
                    t.flush()
                    try:
                        h_zipcontainer(h, t.name, low.rsplit(".", 1)[-1])
                    except zipfile.BadZipFile:
                        text_runs(h, data, f"{fmt}.run")
                continue
            if low.endswith(_IMG_EXT):
                h.images += 1
                image_meta(h, data, f"{fmt}.image")
            elif low.endswith(_TEXTY_EXT) or data[:5] == b"<?xml":
                markup(h, data, f"{fmt}.xml") if data.lstrip()[:1] == b"<" else text_lines(h, data, f"{fmt}.text")
            elif low.endswith((".brp", ".brep")):
                continue  # OCC BRep geometry: no strings of interest
            else:
                text_runs(h, data, f"{fmt}.run")


# ---- PDF -----------------------------------------------------------------------
def h_pdf(h: Harvest, path: str) -> None:
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    with pymupdf.open(path) as doc:
        if doc.needs_pass and not doc.authenticate(""):
            h.notes.append("encrypted pdf (password required): raw text runs only")
            with open(path, "rb") as f:
                text_runs(h, f.read(), "pdf.raw")
            return
        for k, v in (doc.metadata or {}).items():
            if v and k not in ("format", "encryption"):
                h.add("pdf.meta." + k, v)
        try:
            xmp = doc.get_xml_metadata()
            if xmp:
                markup(h, xmp.encode("utf-8"), "pdf.xmp")
        except Exception:  # noqa: BLE001
            pass
        for t in doc.get_toc(simple=True):
            h.add("pdf.toc", t[1])
        try:
            for name in doc.embfile_names():
                h.add("pdf.embedded_file", name)
        except Exception:  # noqa: BLE001
            pass
        for page in doc:
            txt = page.get_text("text")
            for line in txt.splitlines():
                h.add("pdf.text", line)
            n_img = len(page.get_images(full=False))
            h.images += n_img
            if n_img and len(txt.strip()) < 20:
                h.scanned_pages += 1
            for a in page.annots() or []:
                for k in ("content", "title", "subject"):
                    if a.info.get(k):
                        h.add("pdf.annot." + k, a.info[k])
            for w in page.widgets() or []:
                if w.field_value and isinstance(w.field_value, str):
                    h.add("pdf.form", w.field_value)
                if w.field_name:
                    h.add("pdf.form_name", w.field_name)
            for ln in page.get_links():
                if ln.get("uri"):
                    h.add("pdf.link", ln["uri"])


# ---- images: metadata only here (OCR/logo pass is separate) ---------------------------
def image_meta(h: Harvest, data: bytes, kind: str) -> None:
    segs = []
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF:
            marker, ln = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker == 0xDA:
                break
            if 0xE0 <= marker <= 0xEF or marker == 0xFE:
                segs.append(data[i + 4:i + 2 + ln])
            i += 2 + ln
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        i = 8
        while i + 8 <= len(data):
            ln, typ = struct.unpack(">I4s", data[i:i + 8])
            body = data[i + 8:i + 8 + ln]
            if typ in (b"tEXt", b"iTXt", b"eXIf"):
                segs.append(body)
            elif typ == b"zTXt":
                k, _, rest = body.partition(b"\x00")
                try:
                    segs.append(k + b" " + zlib.decompress(rest[1:]))
                except zlib.error:
                    pass
            if typ == b"IEND":
                break
            i += 12 + ln
    else:
        segs.append(data[:65536])
    for s in segs:
        if b"<x:xmpmeta" in s or b"<rdf:" in s:
            markup(h, s, kind + ".xmp")
        else:
            text_runs(h, s, kind + ".meta")


def h_image(h: Harvest, buf) -> None:
    h.images += 1
    image_meta(h, bytes(buf[: min(len(buf), 4_000_000)]), "image")


# ---- DWG via ODA File Converter -> DXF ----------------------------------------------
ODA_VERSION = {"AC1009": "ACAD12", "AC1012": "ACAD13", "AC1014": "ACAD14", "AC1015": "ACAD2000",
               "AC1018": "ACAD2004", "AC1021": "ACAD2007", "AC1024": "ACAD2010", "AC1027": "ACAD2013",
               "AC1032": "ACAD2018"}


def h_dwg(h: Harvest, path: str) -> None:
    ver = Path(path).read_bytes()[:6].decode("latin1")
    with tempfile.TemporaryDirectory() as tin, tempfile.TemporaryDirectory() as tout:
        shutil.copy(path, Path(tin) / "in.dwg")
        env = {**os.environ, "USER": "deid", "LOGNAME": "deid"}
        env.setdefault("DISPLAY", ":99")
        r = subprocess.run(["ODAFileConverter", tin, tout, ODA_VERSION.get(ver, "ACAD2018"), "DXF", "0", "1", "*.*"],
                           capture_output=True, text=True, env=env, timeout=600)
        out = Path(tout) / "in.dxf"
        if not out.exists():
            h.notes.append(f"ODA failed ({ver}): {(r.stdout + r.stderr)[-200:]}")
            text_runs(h, Path(path).read_bytes(), "dwg.raw")
            return
        h_dxf(h, out.read_bytes())
        # DXF strings are re-labelled as dwg.* so Stage 2 knows the source
        for (k, v), n in list(h.strings.items()):
            if k.startswith("dxf."):
                del h.strings[(k, v)]
                h.strings[("dwg." + k[4:], v)] += n
        h.images += 1 if b"THUMBNAILIMAGE" in out.read_bytes()[:10_000_000] else 0


# ---- generic ------------------------------------------------------------------
def looks_text(buf) -> bool:
    s = bytes(buf[:8192])
    if not s:
        return False
    if b"\x00" in s:
        return False
    try:
        s.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        if e.start > len(s) - 4:
            return True
    bad = sum(1 for c in s if c < 9 or 13 < c < 32)
    return bad / len(s) < 0.01


_JSON_STR = re.compile(rb'"((?:[^"\\\r\n]|\\.){2,400})"')


def h_json(h: Harvest, buf, kind: str) -> None:
    import json as _json
    local = Counter(m.group(1) for m in _JSON_STR.finditer(buf))
    for raw, n in local.items():
        try:
            v = _json.loads(b'"' + raw + b'"')
        except ValueError:
            v = decode_bytes(raw)
        h.add(kind, v, n)


def h_generic(h: Harvest, buf, fmt: str) -> None:
    base = fmt.split(":", 1)[-1] if ":" in fmt else fmt
    if looks_text(buf):
        first = bytes(buf[:200]).lstrip()[:1]
        if first in (b"{", b"[") or base in ("json", "geojson", "ipynb"):
            h_json(h, buf, f"json.{base}")
        elif first == b"<":
            markup(h, buf, f"text.{base}")
        else:
            text_lines(h, buf, f"text.{base}")
    else:
        text_runs(h, buf, f"bin.{base}")


def h_jt(h: Harvest, buf) -> None:
    text_runs(h, buf, "jt.run")
    # JT v9+: metadata / property atoms live in zlib-compressed segments
    seen = 0
    for m in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", buf):
        if seen >= 2000:
            break
        try:
            inner = zlib.decompressobj().decompress(bytes(buf[m.start():m.start() + 50_000_000]), 100_000_000)
        except zlib.error:
            continue
        if len(inner) > 16:
            seen += 1
            text_runs(h, inner, "jt.zrun")


MMAP_HANDLERS = {
    "step": h_step, "iges": h_iges, "acis-sat": h_sat, "parasolid-text": h_parasolid_text,
    "stl-ascii": h_stl_ascii, "stl-binary": h_stl_binary, "obj": h_obj, "off": h_off, "vrml": h_vrml,
    "dxf": h_dxf, "postscript": h_postscript, "creo": h_creo, "solidworks-new": h_solidworks,
    "png": h_image, "jpeg": h_image, "tiff": h_image, "jt": h_jt,
    "svg": lambda h, b: markup(h, b, "svg"),
}
PATH_HANDLERS = {"pdf": h_pdf, "dwg": h_dwg}
ZIP_FORMATS = {"fcstd", "3mf", "f3d", "f3z", "odg"}
ARCHIVE_FORMATS = {"zip", "zip-other", "7z", "rar", "tar", "gzip", "unix-compress"}


def harvest_file(path: str, fmt: str) -> Harvest:
    h = Harvest()
    if os.path.getsize(path) == 0:
        return h
    if fmt.startswith("ole2"):
        h_ole(h, path)
    elif fmt in PATH_HANDLERS:
        PATH_HANDLERS[fmt](h, path)
    elif fmt in ZIP_FORMATS:
        try:
            h_zipcontainer(h, path, fmt)
        except zipfile.BadZipFile as e:
            h.notes.append(f"bad zip: {e}")
            with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
                text_runs(h, buf, f"{fmt}.raw")
    else:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            if fmt in MMAP_HANDLERS:
                MMAP_HANDLERS[fmt](h, buf)
            elif fmt.endswith(":gif") or fmt.endswith(":jpg") or fmt.endswith(":bmp"):
                h_image(h, buf)
            else:
                h_generic(h, buf, fmt)
    return h


def harvest_bytes(data: bytes, fmt: str, suffix: str = "") -> Harvest:
    with tempfile.NamedTemporaryFile(suffix=suffix) as t:
        t.write(data)
        t.flush()
        return harvest_file(t.name, fmt)
