"""Stage 3: route one file (blob or archive member) to its writer.

    rewrite_path(src, dst, name, ctx) -> Outcome

Outcome.status:
  ok          dst written (de-identified)
  quarantine  could not be rewritten safely -> NOT shipped, listed in the report
  copy        content has nothing to rewrite (empty) -> dst is a byte copy
  skip        not shipped (truncated '.resume-partial' downloads)
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..formats import HEAD_BYTES, detect
from ..replace import Stats
from .mapping import DeidMap

TEXT_FORMATS = {"step", "iges", "acis-sat", "parasolid-text", "dxf", "svg", "obj", "vrml", "unv", "stl-ascii", "postscript",
                "creo-neutral", "brep", "scad", "off"}
BINARY_FORMATS = {"parasolid-bin", "u3d", "catia", "3ds", "ideas", "eps-binary", "native-prt", "native-asm", "native-drw"}
ZIP_CAD = {"fcstd", "3mf", "f3d", "f3z", "odg"}
ARCHIVES = {"zip", "zip-other", "tar", "gzip", "7z", "rar", "unix-compress"}
IMAGES = {"png", "jpeg", "tiff"}
MAX_DEPTH = 4


@dataclass
class Outcome:
    status: str
    fmt: str
    info: dict = field(default_factory=dict)
    residual: list = field(default_factory=list)
    members: int = 0
    quarantined_members: list = field(default_factory=list)


@dataclass
class Ctx:
    dm: DeidMap
    st: Stats = field(default_factory=Stats)
    depth: int = 0
    tmp: Path | None = None
    in_cad_container: bool = False  # images inside FCStd/3MF/f3d are thumbnails -> neutral
    notes: list = field(default_factory=list)
    logo_cache: dict | None = None  # image sha256 -> [(box, score)] from the logo scan (None = detect live)

    def child(self, **kw) -> "Ctx":
        c = Ctx(self.dm, self.st, self.depth + 1, self.tmp, self.in_cad_container, self.notes, self.logo_cache)
        for k, v in kw.items():
            setattr(c, k, v)
        return c


def _looks_text(head: bytes) -> bool:
    if not head:
        return False
    if b"\x00" in head[:4096]:
        return False
    try:
        head[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        if e.start > len(head[:4096]) - 4:
            return True
    printable = sum(32 <= c < 127 or c in (9, 10, 13) or c >= 0x80 for c in head[:4096])
    return printable / max(1, len(head[:4096])) > 0.95


def rewrite_path(src: Path, dst: Path, name: str, ctx: Ctx) -> Outcome:
    with open(src, "rb") as f:
        head = f.read(HEAD_BYTES)
    fmt = detect(name, head)
    fam = fmt.split(":")[0]
    dm, st = ctx.dm, ctx.st
    try:
        if fam == "partial-download":
            return Outcome("skip", fmt)
        if fam == "empty":
            shutil.copyfile(src, dst)
            return Outcome("copy", fmt)
        if ctx.depth > MAX_DEPTH:
            return Outcome("quarantine", fmt, dict(error="nesting too deep"))
        if fam in TEXT_FORMATS or fam in ("unknown",) and _looks_text(head):
            from .text import rewrite_text
            rewrite_text(fam, src, dst, dm, st)
            return _done(dst, fmt, ctx)
        if fam == "stl-binary":
            from .text import rewrite_stl_binary
            rewrite_stl_binary(src, dst, dm, st)
            return Outcome("ok", fmt)
        if fam in ZIP_CAD or fam in ARCHIVES:
            from .containers import rewrite_archive
            return rewrite_archive(fam, src, dst, name, ctx)
        if fam == "pdf":
            from .pdf import rewrite_pdf
            return rewrite_pdf(src, dst, ctx)
        if fam in IMAGES:
            from .image import rewrite_image
            return rewrite_image(src, dst, fmt, ctx)
        if fam == "dwg":
            from .dwg import rewrite_dwg
            return rewrite_dwg(src, dst, ctx)
        if fam == "dxf":
            from .text import rewrite_text
            rewrite_text("dxf", src, dst, dm, st)
            return _done(dst, fmt, ctx)
        # ---- in-memory, length-coupled binaries ----
        data = src.read_bytes()
        if fam == "solidworks-new":
            from . import sw
            new, info = sw.rewrite(data, dm, st)
        elif fam == "ole2":
            from . import ole
            new, info = ole.rewrite(data, dm, st)
        elif fam == "creo":
            from .binary import rewrite_creo
            new, info = rewrite_creo(data, dm, st)
        elif fam == "jt":
            from .binary import rewrite_binary
            new, info = rewrite_binary(data, dm, st, inflate=True)
        else:  # BINARY_FORMATS, unknown binary
            from .binary import rewrite_binary
            new, info = rewrite_binary(data, dm, st)
        if new is None:
            return Outcome("quarantine", fmt, info)
        dst.write_bytes(new)
        return Outcome("ok", fmt, info, residual=dm.residual(new) if len(new) < 300_000_000 else [])
    except Exception as e:  # noqa: BLE001 - one bad file must never stop the run
        import traceback
        return Outcome("quarantine", fmt, dict(error=f"{type(e).__name__}: {e}"[:300],
                                               trace=traceback.format_exc()[-1500:]))


def _done(dst: Path, fmt: str, ctx: Ctx) -> Outcome:
    size = dst.stat().st_size
    res = []
    if size < 200_000_000:
        res = ctx.dm.residual(dst.read_bytes())
    return Outcome("ok", fmt, residual=res)


def temp_dir(ctx: Ctx) -> Path:
    return Path(tempfile.mkdtemp(prefix="deid-", dir=ctx.tmp))
