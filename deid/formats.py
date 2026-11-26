"""File-format detection from key name + leading bytes (magic), not extension alone.

`detect(key, head)` returns a short format id used to route files to harvesters
and writers. `head` should be the first ~64+ bytes of the object (512 is better
for ASCII-vs-binary STL).
"""
from __future__ import annotations

import re

HEAD_BYTES = 512

ARCHIVE_EXTS = {"zip", "7z", "rar", "tar", "gz", "tgz", "z"}
_NUMERIC_EXT = re.compile(r"^\d+$")

_MAGIC = [
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),
    (b"%PDF", "pdf"),
    (b"ISO-10303-21", "step"),
    (b"#UGC:", "creo"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"\x1f\x8b", "gzip"),
    (b"\x1f\x9d", "unix-compress"),
    (b"%!PS", "postscript"),
    (b"\xc5\xd0\xd3\xc6", "eps-binary"),
    (b"AC10", "dwg"),
    (b"PS\x00\x00", "parasolid-bin"),
]

# ZIP-based formats, distinguished by extension after the PK magic.
_ZIP_EXT = {"fcstd": "fcstd", "3mf": "3mf", "f3d": "f3d", "f3z": "f3z", "odg": "odg", "zip": "zip"}


def split_ext(key: str) -> tuple[str, str]:
    """Return (ext, base_ext). For Creo-style `part.prt.3` -> ('3', 'prt').

    `base_ext` is the meaningful type extension (== ext for normal files).
    """
    name = key.rsplit("/", 1)[-1]
    if "." not in name:
        return "", ""
    parts = name.lower().split(".")
    ext = parts[-1]
    if _NUMERIC_EXT.match(ext) and len(parts) >= 3:
        return ext, parts[-2]
    return ext, ext


def detect(key: str, head: bytes) -> str:
    ext, base = split_ext(key)
    if not head:
        return "empty"
    if ext == "resume-partial":
        return "partial-download"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return _ZIP_EXT.get(base, "zip-other")
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            if fmt == "ole2":
                return f"ole2:{base}" if base else "ole2"
            return fmt
    # SolidWorks 2015+ container: 4 varying bytes then 00 00 00 04.
    if base in {"sldprt", "sldasm", "slddrw", "slddrt", "sldlfp", "prtdot", "asmdot", "drwdot"} and head[4:8] == b"\x00\x00\x00\x04":
        return "solidworks-new"
    if base == "stl":
        txt = head[:HEAD_BYTES].decode("ascii", "ignore").lower()
        if txt.lstrip().startswith("solid") and ("facet" in txt or "endsolid" in txt or len(head) < 84):
            return "stl-ascii"
        return "stl-binary"
    if base == "tar" or (len(head) >= 262 and head[257:262] == b"ustar"):
        return "tar"
    text_like = {
        "step": "step", "stp": "step", "iges": "iges", "igs": "iges", "sat": "acis-sat",
        "x_t": "parasolid-text", "xmt_txt": "parasolid-text", "x_b": "parasolid-bin", "xmt_bin": "parasolid-bin",
        "dxf": "dxf", "svg": "svg", "obj": "obj", "wrl": "vrml", "unv": "unv", "scad": "scad",
        "off": "off", "neu": "creo-neutral", "ps": "postscript", "eps": "postscript", "brep": "brep", "brp": "brep",
        "jt": "jt", "u3d": "u3d", "3ds": "3ds", "catpart": "catia", "catproduct": "catia", "catdrawing": "catia",
        "mf1": "ideas", "dwg": "dwg",
    }
    if base in text_like:
        return text_like[base]
    if base in {"prt", "asm", "drw"}:
        return f"native-{base}"
    return f"unknown:{base}" if base else "unknown"
