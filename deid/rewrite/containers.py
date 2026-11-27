"""Containers: zip-based CAD (FCStd / 3MF / f3d / f3z / ODG) and archives (zip, tar, gzip, 7z, rar, .Z).

Each member is written to a private temp file under an index name (member names never touch the
filesystem -> no zip-slip), rewritten by its own format (recursive dispatch), renamed with the same
pseudonym map as object keys (`DeidMap.key`), and repacked:
  zip / CAD zip   same member order, compression method and timestamps; comments mapped
  tar             same order and modes; uname/gname cleared (they are account names)
  gzip            FNAME header mapped
  7z              repacked as 7z;  rar -> repacked as **zip** under the same key (rar cannot be written)
  .Z              recompressed with ncompress
Thumbnails inside CAD zips are replaced by neutral images. Encrypted members or archives ->
quarantine (not shipped). A quarantined member quarantines its container: a partially de-identified
archive is never shipped.
"""
from __future__ import annotations

import gzip
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from ..unpack import Budget, Quarantine
from .dispatch import Ctx, Outcome, rewrite_path, temp_dir
from .thumbs import neutral_any_size

THUMB = re.compile(r"(?i)(^|/)(thumbnails?/|metadata/thumbnail|.*thumbnail[^/]*\.(png|jpe?g|bmp)$|.*preview[^/]*\.(png|jpe?g|bmp)$)")


def _member(src: Path, name: str, ctx: Ctx, work: Path, idx: int) -> tuple[Path | None, Outcome]:
    dst = work / f"o{idx}"
    if ctx.in_cad_container and THUMB.search(name):
        n = neutral_any_size(src.read_bytes())
        if n is not None:
            dst.write_bytes(n)
            return dst, Outcome("ok", "thumbnail", dict(neutral=True))
    o = rewrite_path(src, dst, name, ctx)
    if o.status in ("ok", "copy"):
        return dst, o
    return None, o


def _finish(out: Outcome, sub: Outcome, name: str):
    out.members += 1 + sub.members
    out.residual += [f"{name}: {r}" for r in sub.residual[:5]]
    if sub.status == "quarantine":
        out.quarantined_members.append((name, sub.info.get("error") or str(sub.info)[:200]))
    out.quarantined_members += sub.quarantined_members


def rewrite_zip(src: Path, dst: Path, ctx: Ctx, fmt: str) -> Outcome:
    work = temp_dir(ctx)
    out = Outcome("ok", fmt)
    budget = Budget(src.stat().st_size)
    sub_ctx = ctx.child(in_cad_container=ctx.in_cad_container or fmt in ("fcstd", "3mf", "f3d", "f3z", "odg"))
    try:
        with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", allowZip64=True) as zo:
            zo.comment = ctx.dm.apply(zi.comment)
            for i, info in enumerate(zi.infolist()):
                if info.flag_bits & 0x1:
                    raise Quarantine("encrypted zip member")
                from ..unpack import decode_zip_name
                real = decode_zip_name(info)
                new_name = ctx.dm.key(real)
                ni = zipfile.ZipInfo(new_name, date_time=info.date_time)
                ni.compress_type, ni.external_attr, ni.create_system = info.compress_type, info.external_attr, info.create_system
                ni.comment = ctx.dm.apply(info.comment)
                if new_name != info.filename and not new_name.isascii():
                    ni.flag_bits |= 0x800  # UTF-8 names
                if info.is_dir():
                    zo.writestr(ni, b"")
                    continue
                budget.add_member()
                tmp = work / f"i{i}"
                with zi.open(info) as fsrc, open(tmp, "wb") as fdst:
                    n = 0
                    while chunk := fsrc.read(1 << 20):
                        n += len(chunk)
                        budget.add_bytes(len(chunk), n)
                        fdst.write(chunk)
                res, sub = _member(tmp, real, sub_ctx, work, i)
                _finish(out, sub, real)
                if res is None:
                    if sub.status == "skip":
                        continue
                    out.status = "quarantine"
                    out.info["error"] = f"member quarantined: {real}"
                    return out
                with open(res, "rb") as fr, zo.open(ni, "w", force_zip64=res.stat().st_size > 2**31) as fw:
                    shutil.copyfileobj(fr, fw, 1 << 20)
                tmp.unlink(missing_ok=True)
                res.unlink(missing_ok=True)
        return out
    except NotImplementedError:  # Deflate64 / other methods zipfile cannot read: 7z extracts, we write deflate
        shutil.rmtree(work, ignore_errors=True)
        o = rewrite_7z(src, dst, ctx, "zip")
        o.fmt = fmt
        o.info["note"] = "unsupported zip compression method: rebuilt via 7z as deflate"
        return o
    except (Quarantine, zipfile.BadZipFile, RuntimeError) as e:
        return Outcome("quarantine", fmt, dict(error=f"{type(e).__name__}: {e}"[:300]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def rewrite_tar(src: Path, dst: Path, ctx: Ctx) -> Outcome:
    work = temp_dir(ctx)
    out = Outcome("ok", "tar")
    budget = Budget(src.stat().st_size)
    try:
        mode = "w"
        head = src.open("rb").read(6)
        if head[:2] == b"\x1f\x8b":
            mode = "w:gz"
        elif head[:3] == b"BZh":
            mode = "w:bz2"
        elif head[:6] == b"\xfd7zXZ\x00":
            mode = "w:xz"
        with tarfile.open(src, "r:*") as ti, tarfile.open(dst, mode, format=tarfile.PAX_FORMAT) as to:
            for i, m in enumerate(ti):
                nm = ctx.dm.key(m.name)
                m2 = tarfile.TarInfo(nm)
                m2.mode, m2.mtime, m2.type, m2.uid, m2.gid = m.mode, m.mtime, m.type, 0, 0
                m2.uname = m2.gname = ""
                if m.isdir():
                    to.addfile(m2)
                    continue
                if not m.isreg():
                    out.info.setdefault("dropped_links", []).append(nm)  # symlinks / devices never shipped
                    continue
                budget.add_member()
                tmp = work / f"i{i}"
                with ti.extractfile(m) as fsrc, open(tmp, "wb") as fdst:
                    n = 0
                    while chunk := fsrc.read(1 << 20):
                        n += len(chunk)
                        budget.add_bytes(len(chunk), n)
                        fdst.write(chunk)
                res, sub = _member(tmp, m.name, ctx.child(), work, i)
                _finish(out, sub, m.name)
                if res is None:
                    if sub.status == "skip":
                        continue
                    return Outcome("quarantine", "tar", dict(error=f"member quarantined: {m.name}"),
                                   quarantined_members=out.quarantined_members)
                m2.size = res.stat().st_size
                with open(res, "rb") as fr:
                    to.addfile(m2, fr)
                tmp.unlink(missing_ok=True)
                res.unlink(missing_ok=True)
        return out
    except (Quarantine, tarfile.TarError, EOFError, OSError) as e:
        return Outcome("quarantine", "tar", dict(error=f"{type(e).__name__}: {e}"[:300]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _single(src: Path, dst: Path, name: str, ctx: Ctx, fmt: str) -> Outcome:
    """gzip / .Z: one inner stream."""
    from ..unpack import _single_stream_name, recover_raw_gzip
    work = temp_dir(ctx)
    try:
        inner_name = _single_stream_name(name.rsplit("/", 1)[-1], (".tgz", ".gz", ".z"))
        if name.lower().endswith(".tgz"):
            inner_name += ".tar"
        tmp = work / "inner"
        fname = None
        if fmt == "gzip":
            raw = src.read_bytes()
            if len(raw) > 10 and raw[3] & 8:  # FNAME present
                p = 10 + (2 + int.from_bytes(raw[10:12], "little") if raw[3] & 4 else 0)
                fname = raw[p:raw.index(b"\0", p)].decode("latin-1")
            try:
                with gzip.open(src, "rb") as g, open(tmp, "wb") as f:
                    shutil.copyfileobj(g, f, 1 << 20)
            except (gzip.BadGzipFile, EOFError, OSError):
                tmp.unlink(missing_ok=True)
                recover_raw_gzip(src, tmp, Budget(src.stat().st_size))
        else:
            import ncompress
            tmp.write_bytes(ncompress.decompress(src.read_bytes()))
        out = Outcome("ok", fmt)
        res, sub = _member(tmp, (fname or inner_name), ctx.child(), work, 0)
        _finish(out, sub, fname or inner_name)
        if res is None:
            return Outcome("quarantine", fmt, dict(error=f"inner quarantined: {sub.info}"[:300]),
                           quarantined_members=out.quarantined_members)
        if fmt == "gzip":
            mtime = int.from_bytes(src.read_bytes()[4:8], "little")
            with open(res, "rb") as fr, open(dst, "wb") as fo:
                with gzip.GzipFile(filename=ctx.dm.key(fname) if fname else "", mode="wb", fileobj=fo, mtime=mtime) as g:
                    shutil.copyfileobj(fr, g, 1 << 20)
        else:
            import ncompress
            dst.write_bytes(ncompress.compress(res.read_bytes()))
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def rewrite_7z(src: Path, dst: Path, ctx: Ctx, fmt: str) -> Outcome:
    """7z / rar: extract with the Stage 0 guards, rewrite the tree, repack (rar -> zip)."""
    from ..unpack import extract_7z
    work = temp_dir(ctx)
    try:
        ex = work / "x"
        ex.mkdir()
        try:
            extract_7z(src, ex, Budget(src.stat().st_size), [])
        except Quarantine as e:
            return Outcome("quarantine", fmt, dict(error=str(e)[:300]))
        as_zip = fmt in ("rar", "zip")
        out = Outcome("ok", fmt, dict(repacked_as="zip" if as_zip else "7z"))
        stage = work / "y"
        files = sorted(p for p in ex.rglob("*") if p.is_file())
        for i, p in enumerate(files):
            rel = p.relative_to(ex).as_posix()
            res, sub = _member(p, rel, ctx.child(), work, i)
            _finish(out, sub, rel)
            if res is None:
                if sub.status == "skip":
                    continue
                return Outcome("quarantine", fmt, dict(error=f"member quarantined: {rel}"),
                               quarantined_members=out.quarantined_members)
            target = stage / ctx.dm.key(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(res, target)
        stage.mkdir(exist_ok=True)
        if as_zip:
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zo:
                for p in sorted(stage.rglob("*")):
                    if p.is_file():
                        zo.write(p, p.relative_to(stage).as_posix())
        else:
            tmp7 = work / "out.7z"
            r = subprocess.run(["7z", "a", "-t7z", "-mx=7", "-bd", "-y", str(tmp7), "."], cwd=stage,
                               capture_output=True, text=True, timeout=7200)
            if r.returncode != 0:
                return Outcome("quarantine", fmt, dict(error=f"7z repack failed: {r.stderr[:200]}"))
            shutil.move(tmp7, dst)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def rewrite_archive(fam: str, src: Path, dst: Path, name: str, ctx: Ctx) -> Outcome:
    if fam in ("zip", "zip-other", "fcstd", "3mf", "f3d", "f3z", "odg"):
        return rewrite_zip(src, dst, ctx, fam)
    if fam == "tar":
        return rewrite_tar(src, dst, ctx)
    if fam in ("gzip", "unix-compress"):
        return _single(src, dst, name, ctx, fam)
    if fam in ("7z", "rar"):
        return rewrite_7z(src, dst, ctx, fam)
    return Outcome("quarantine", fam, dict(error="unsupported archive"))
