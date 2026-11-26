"""Unpack safety guards: zip-slip, absolute paths, symlinks, bombs, GBK names, .Z."""
import gzip
import io
import subprocess
import tarfile
import zipfile

import ncompress
import pytest

from deid.unpack import Budget, Quarantine, decode_zip_name, extract


def run(fmt, path, tmp_path, name="a"):
    dest = tmp_path / "out"
    dest.mkdir()
    notes = []
    extract(fmt, path, dest, Budget(path.stat().st_size), notes, name)
    return dest, notes


def test_zip_slip_rejected(tmp_path):
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("../../etc/evil.txt", "x")
    with pytest.raises(Quarantine, match="traversal"):
        run("zip", p, tmp_path)
    assert not (tmp_path.parent / "etc").exists()


def test_zip_absolute_rejected(tmp_path):
    p = tmp_path / "abs.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("/tmp/abs.txt", "x")
    with pytest.raises(Quarantine, match="absolute"):
        run("zip", p, tmp_path)


def test_tar_symlink_skipped_not_created(tmp_path):
    p = tmp_path / "link.tar"
    with tarfile.open(p, "w") as t:
        ti = tarfile.TarInfo("link")
        ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(ti)
        data = b"hello"
        ti2 = tarfile.TarInfo("ok/file.step")
        ti2.size = len(data)
        t.addfile(ti2, io.BytesIO(data))
    dest, notes = run("tar", p, tmp_path)
    assert not (dest / "link").exists() and not (dest / "link").is_symlink()
    assert (dest / "ok/file.step").read_bytes() == b"hello"
    assert any("non-regular" in n for n in notes)


def test_gzip_bomb_quarantined(tmp_path):
    p = tmp_path / "bomb.gz"
    with gzip.open(p, "wb") as g:
        for _ in range(130):  # 130 MiB of zeros -> ~130 KB compressed (ratio ~1000x)
            g.write(b"\0" * 1024 * 1024)
    with pytest.raises(Quarantine, match="ratio"):
        run("gzip", p, tmp_path, name="bomb.gz")


def test_gbk_zip_name_decoded(tmp_path):
    # Legacy Chinese zip: GBK name bytes, UTF-8 flag NOT set. zipfile always writes
    # non-ASCII names as UTF-8, so write an ASCII placeholder and patch the bytes.
    gbk = "青龙.step".encode("gbk")
    placeholder = b"X" * (len(gbk) - 5) + b".step"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(placeholder.decode(), "data")
    p = tmp_path / "cn.zip"
    p.write_bytes(buf.getvalue().replace(placeholder, gbk))
    with zipfile.ZipFile(p) as z:
        info = z.infolist()[0]
        assert not info.flag_bits & 0x800
        assert decode_zip_name(info) == "青龙.step"


def test_windows_style_zip_member_extracted(tmp_path):
    p = tmp_path / "win.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("dir/part.SLDPRT", "data")  # external_attr has perms only, no S_IFREG
    dest, _ = run("zip", p, tmp_path)
    assert (dest / "dir/part.SLDPRT").read_bytes() == b"data"


def test_unix_compress_roundtrip(tmp_path):
    p = tmp_path / "part.prt.Z"
    p.write_bytes(ncompress.compress(b"#UGC:2 PART " * 100))
    dest, _ = run("unix-compress", p, tmp_path, name="part.prt.Z")
    assert (dest / "part.prt").read_bytes().startswith(b"#UGC:2")


def test_7z_symlink_rejected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lnk").symlink_to("/etc/passwd")
    p = tmp_path / "l.7z"
    subprocess.run(["7z", "a", "-snl", str(p), str(src / "lnk")], check=True, capture_output=True)
    with pytest.raises(Quarantine, match="symlink"):
        run("7z", p, tmp_path)


def test_gzip_bad_crc_recovered(tmp_path):
    import zlib as _z
    payload = b"I-DEAS export /scratch2/user/part.mf1\n" * 200
    g = bytearray(gzip.compress(payload))
    g[-8:-4] = (_z.crc32(payload) ^ 0xFFFF).to_bytes(4, "little")  # corrupt the CRC
    p = tmp_path / "x.arc.gz"
    p.write_bytes(bytes(g))
    dest, notes = run("gzip", p, tmp_path, name="x.arc.gz")
    assert (dest / "x.arc").read_bytes() == payload
    assert any(n.startswith("recovered:") for n in notes)
