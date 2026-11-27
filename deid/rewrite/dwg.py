"""DWG writer: ODA File Converter round trip at the file's own version (see docs/SPIKES.md).

DWG --ODA--> DXF (same version) --text writer + THUMBNAILIMAGE neutralised--> DXF --ODA--> DWG.
The spike verified: version kept, entity counts per type identical, coordinate multiset identical.
ODA stamps $LASTSAVEDBY with the OS account running it; this box's account is the generic `ubuntu`
(the Docker image runs as `deid`). DWG summary info (author ...) does not survive the round trip,
which only removes data. Files with a non-DWG header go to the generic binary writer.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .dispatch import Ctx, Outcome, temp_dir
from .thumbs import neutral_bmp

ODA_VERSION = {"AC1006": "ACAD10", "AC1009": "ACAD12", "AC1012": "ACAD13", "AC1014": "ACAD14", "AC1015": "ACAD2000",
               "AC1018": "ACAD2004", "AC1021": "ACAD2007", "AC1024": "ACAD2010", "AC1027": "ACAD2013", "AC1032": "ACAD2018"}


def _oda(src_dir: Path, dst_dir: Path, version: str, out_type: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "USER": "deid", "LOGNAME": "deid"}
    env.setdefault("DISPLAY", ":99")
    return subprocess.run(["ODAFileConverter", str(src_dir), str(dst_dir), version, out_type, "0", "1", "*.*"],
                          capture_output=True, text=True, env=env, timeout=900)


_THUMB = re.compile(rb"(\n\s*2\r?\nTHUMBNAILIMAGE\r?\n)(.*?)(\n\s*0\r?\nENDSEC)", re.S)


def neutral_thumbnail(dxf: bytes) -> tuple[bytes, bool]:
    m = _THUMB.search(dxf)
    if not m:
        return dxf, False
    body = m.group(2)
    lines = body.split(b"\n")
    idx = [i for i in range(0, len(lines) - 1) if lines[i].strip() == b"310"]
    hexdata = b"".join(lines[i + 1].strip() for i in idx)
    try:
        bmp = bytes.fromhex(hexdata.decode())
    except ValueError:
        return dxf, False
    nb = neutral_bmp(bmp)
    if nb is None:
        return dxf, False
    h = nb.hex().upper().encode()
    pos = 0
    for i in idx:
        old = lines[i + 1]
        n = len(old.strip())
        cr = old[len(old.rstrip(b"\r")):]
        lines[i + 1] = h[pos:pos + n] + cr
        pos += n
    return dxf[:m.start(2)] + b"\n".join(lines) + dxf[m.end(2):], True


def rewrite_dwg(src: Path, dst: Path, ctx: Ctx) -> Outcome:
    ver = src.read_bytes()[:6].decode("latin1", "replace")
    if ver not in ODA_VERSION:
        from .binary import rewrite_binary
        new, info = rewrite_binary(src.read_bytes(), ctx.dm, ctx.st)
        if new is None:
            return Outcome("quarantine", "dwg", info)
        dst.write_bytes(new)
        return Outcome("ok", "dwg", dict(info, note=f"non-DWG header {ver!r}: generic binary writer"))
    work = temp_dir(ctx)
    try:
        a, b, c = work / "a", work / "b", work / "c"
        for d in (a, b, c):
            d.mkdir()
        shutil.copy(src, a / "f.dwg")
        r = _oda(a, b, ODA_VERSION[ver], "DXF")
        dxf = b / "f.dxf"
        if not dxf.exists():
            return Outcome("quarantine", "dwg", dict(error=f"ODA DWG->DXF failed: {(r.stdout + r.stderr)[-200:]}"))
        from .text import dxf_fn
        data = dxf.read_bytes()
        new = dxf_fn(ctx.dm, ctx.st)(data)
        new, thumb = neutral_thumbnail(new)
        (c / "f.dxf").write_bytes(new)
        out_dir = work / "d"
        out_dir.mkdir()
        r = _oda(c, out_dir, ODA_VERSION[ver], "DWG")
        out = out_dir / "f.dwg"
        if not out.exists() or out.read_bytes()[:6].decode("latin1", "replace") != ver:
            return Outcome("quarantine", "dwg", dict(error=f"ODA DXF->DWG failed: {(r.stdout + r.stderr)[-200:]}"))
        shutil.copy(out, dst)
        res = ctx.dm.residual(new)
        return Outcome("ok", "dwg", dict(version=ver, thumbnail=thumb, size_ratio=round(out.stat().st_size / src.stat().st_size, 2)),
                       residual=res)
    finally:
        shutil.rmtree(work, ignore_errors=True)
