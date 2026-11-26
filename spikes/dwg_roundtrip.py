"""Spike: DWG de-identification via ODA File Converter round trip.

Per file:  DWG --(ODA)--> DXF A  (same AutoCAD version as the input)
  no-op :  A --> DWG B --> DXF B2           (what does ODA change on its own?)
  de-id :  A --replace--> C --> DWG C --> DXF C2
Compare A vs B2 and A vs C2: DWG version, entity counts per type, coordinate multiset
hash, text diff; scan final DWG + DXF for the replaced strings (ASCII/UTF-16/UTF-8).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ODA_VERSION = {"AC1006": "ACAD10", "AC1009": "ACAD12", "AC1012": "ACAD13", "AC1014": "ACAD14",
               "AC1015": "ACAD2000", "AC1018": "ACAD2004", "AC1021": "ACAD2007", "AC1024": "ACAD2010",
               "AC1027": "ACAD2013", "AC1032": "ACAD2018"}
COORD_CODES = {str(c) for c in list(range(10, 19)) + list(range(20, 29)) + list(range(30, 38))}
NEUTRAL_USER = "deid"


def oda(src: Path, dst_dir: Path, version: str, out_type: str) -> Path:
    """Convert one file with ODA (it works on folders, so stage the file alone)."""
    with tempfile.TemporaryDirectory() as tin:
        shutil.copy(src, tin)
        dst_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "USER": NEUTRAL_USER, "LOGNAME": NEUTRAL_USER}
        r = subprocess.run(["ODAFileConverter", tin, str(dst_dir), version, out_type, "0", "1", "*.*"],
                           capture_output=True, text=True, env=env, timeout=600)
        out = dst_dir / (src.stem + "." + out_type.lower())
        if not out.exists():
            raise RuntimeError(f"ODA produced no output for {src.name}: {r.stdout[-300:]} {r.stderr[-300:]}")
        err = dst_dir / (src.stem + "." + out_type.lower() + ".err")
        return out


def dxf_pairs(p: Path):
    t = p.read_bytes().decode("utf-8", "replace").splitlines()
    return [(t[i].strip(), t[i + 1]) for i in range(0, len(t) - 1, 2)]


def summarize(p: Path) -> dict:
    P = dxf_pairs(p)
    types, coords, texts, ent = Counter(), [], [], None
    section = None
    hdr = {}
    for i, (c, v) in enumerate(P):
        if c == "0":
            ent = v
            if v not in ("SECTION", "ENDSEC", "EOF"):
                types[(section, v)] += 1
        elif c == "2" and ent == "SECTION":
            section = v
        elif c == "9":
            hdr[v] = P[i + 1][1] if i + 1 < len(P) else None
        if section in ("ENTITIES", "BLOCKS") and c in COORD_CODES:
            try:
                coords.append(round(float(v), 6))
            except ValueError:
                pass
        if c in ("1", "3") and ent in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
            texts.append(v)
    ent_counts = {f"{s}:{t}": n for (s, t), n in types.items() if s in ("ENTITIES", "BLOCKS")}
    return dict(acadver=hdr.get("$ACADVER"), lastsavedby=hdr.get("$LASTSAVEDBY"), entity_counts=ent_counts,
                n_entities=sum(ent_counts.values()),
                coord_hash=hashlib.sha256(json.dumps(sorted(coords)).encode()).hexdigest()[:16], n_coords=len(coords),
                texts=texts)


def replace_in_dxf(src: Path, dst: Path, mapping: dict[str, str]) -> int:
    t = src.read_bytes().decode("utf-8")
    n = 0
    for k, v in mapping.items():
        n += t.count(k)
        t = t.replace(k, v)
    dst.write_bytes(t.encode("utf-8"))
    return n


def leaks(paths: list[Path], needles: list[str]) -> dict:
    out = {}
    for p in paths:
        raw = p.read_bytes()
        hit = [n for n in needles if n.encode("utf-8") in raw or n.encode("utf-16le") in raw
               or (n.isascii() and n.encode() in raw)]
        out[p.name] = hit
    return out


def run(dwg: Path, work: Path, mapping: dict[str, str]) -> dict:
    head = dwg.read_bytes()[:6].decode("ascii", "replace")
    ver = ODA_VERSION.get(head)
    if not ver:
        return dict(file=dwg.name, error=f"unsupported/unknown DWG header {head!r}")
    w = work / dwg.stem
    shutil.rmtree(w, ignore_errors=True)
    a = oda(dwg, w / "A", ver, "DXF")
    b = oda(oda(a, w / "B", ver, "DWG"), w / "B2", ver, "DXF")
    c_dxf = w / "C" / a.name
    c_dxf.parent.mkdir(parents=True)
    n_rep = replace_in_dxf(a, c_dxf, mapping)
    c_dwg = oda(c_dxf, w / "Cdwg", ver, "DWG")
    c2 = oda(c_dwg, w / "C2", ver, "DXF")
    sa, sb, sc = summarize(a), summarize(b), summarize(c2)
    out_ver = c_dwg.read_bytes()[:6].decode("ascii", "replace")
    diff_texts = [(x, y) for x, y in zip(sa["texts"], sc["texts"]) if x != y]
    return dict(
        file=dwg.name, in_version=head, out_version=out_ver, version_kept=out_ver == head,
        size_in=dwg.stat().st_size, size_out=c_dwg.stat().st_size,
        noop_same_entities=sa["entity_counts"] == sb["entity_counts"], noop_same_coords=sa["coord_hash"] == sb["coord_hash"],
        deid_same_entities=sa["entity_counts"] == sc["entity_counts"], deid_same_coords=sa["coord_hash"] == sc["coord_hash"],
        n_entities=sa["n_entities"], n_coords=sa["n_coords"], replacements=n_rep,
        changed_texts=diff_texts[:4], lastsavedby_out=sc["lastsavedby"],
        leaks_in_output=leaks([c_dwg, c2], list(mapping)),
        leaks_in_original=leaks([dwg], list(mapping)),
    )


if __name__ == "__main__":
    work = Path(sys.argv[1])
    mapping = json.loads(Path(sys.argv[2]).read_text())
    for f in sys.argv[3:]:
        try:
            print(json.dumps(run(Path(f), work, mapping), ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            print(json.dumps(dict(file=f, error=repr(e)[:400])))
