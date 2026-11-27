"""Stage 3: the pseudonym map as three byte-level replacers plus key renaming.

Scopes (from Stage 2, `entities/map.parquet`):
  global     word-bounded anywhere                       -> Replacer(mode='global')
  field      a complete field value or a path component  -> Replacer(mode='whole')
  path       a path component only (keys <= 3 chars)     -> Replacer(mode='component')

Every pseudonym has the same width as its original in UTF-8 / UTF-16 / cp1252 / GBK, so all
three passes are length-preserving and object-key renames equal the in-file references byte
for byte (an assembly keeps resolving its renamed parts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import pyarrow.parquet as pq

from ..config import DATA_DIR
from ..replace import Replacer, Stats

ENT_DIR = DATA_DIR / "entities"


@dataclass
class Result:
    stats: Stats = field(default_factory=Stats)

    def add(self, st: Stats):
        self.stats.merge(st)


REF_STEM = __import__("re").compile(r"(?=.*[0-9_-])[A-Za-z0-9_-]{8,}")


def ref_stems(pairs) -> dict[str, str]:
    """(old key, new key) -> {old stem: new stem} for distinctive renamed file stems (see module doc)."""
    out = {}
    for k, nk in pairs:
        if k == nk:
            continue
        a, b = k.rsplit("/", 1)[-1].split(".", 1)[0], nk.rsplit("/", 1)[-1].split(".", 1)[0]
        if a != b and len(a) == len(b) and REF_STEM.fullmatch(a):
            out.setdefault(a, b)
    return out


class DeidMap:
    def __init__(self, rows: list[dict], refs: dict[str, str] | None = None):
        by = {"global": {}, "field": {}, "path": {}}
        for r in rows:
            if r["widths_ok"] and r["surface"] != r["pseudonym"]:
                by[r["scope"]].setdefault(r["surface"], r["pseudonym"])
        self.maps = by
        self.passes = [Replacer(by["global"], "global"), Replacer(by["field"], "whole"),
                       Replacer(by["path"], "component")]
        # whole-value lookups for parsed metadata fields (OLE property sets, PDF info, STEP header ...)
        self._whole = {}
        for scope in ("path", "field", "global"):
            for k, v in by[scope].items():
                self._whole.setdefault(k.casefold() if k.isascii() else k, v)
        self.refs = Replacer(refs, "global") if refs else None  # applied first (see module doc)
        self.max_key_bytes = max([p.max_key_bytes for p in self.passes] + ([self.refs.max_key_bytes] if self.refs else []))

    @classmethod
    def load(cls, path=None, keys_path=None, with_refs: bool = True) -> "DeidMap":
        """keys_path: rewrite/keys.parquet from `rewrite_run plan` (reference stems); skipped if absent.
        `plan` itself loads with_refs=False (key renaming never depends on a previous plan)."""
        refs = None
        kp = keys_path or DATA_DIR / "rewrite" / "keys.parquet"
        if with_refs and kp.exists():
            t = pq.read_table(kp, columns=["key", "new_key"])
            refs = ref_stems(zip(t.column(0).to_pylist(), t.column(1).to_pylist()))
        return cls(pq.read_table(path or ENT_DIR / "map.parquet",
                                 columns=["surface", "pseudonym", "scope", "widths_ok"]).to_pylist(), refs)

    # ---- bytes ----------------------------------------------------------
    def apply(self, data: bytes, st: Stats | None = None) -> bytes:
        for p in ([self.refs] if self.refs else []) + self.passes:
            data, s = p.apply(data)
            if st is not None:
                st.merge(s)
        if b"ost" in data or b"OST" in data or b"achine" in data:
            data = self.hosts(data, st)
        return data

    _HOST = __import__("re").compile(rb"(?i)(host(?:name)?\s*[:=]\s*'?|#- HOST |COMPUTERNAME=|machine(?:name)?\s*[:=]\s*)"
                                     rb"([A-Za-z0-9._-]{4,63})")

    def hosts(self, data: bytes, st: Stats | None = None) -> bytes:
        """Machine names glue user / org names into one token ('QUORVEXDESKTOP'): substring rule there."""
        out = bytearray(data)
        for m in self._HOST.finditer(data):
            v = m.group(2)
            nv, n = self.passes[0].substring(v)
            if n:
                out[m.start(2):m.end(2)] = nv
                if st is not None:
                    st.replaced += n
        return bytes(out)

    def residual(self, data: bytes) -> list[str]:
        return self.passes[0].residual(data)

    # ---- text -----------------------------------------------------------
    def text(self, s: str, st: Stats | None = None) -> str:
        """Free text (a decoded string): same rules, applied to its UTF-8 bytes."""
        if not s:
            return s
        return self.apply(s.encode("utf-8", "surrogatepass"), st).decode("utf-8", "surrogatepass")

    def value(self, s: str, st: Stats | None = None) -> str:
        """A complete field value (author, last saved by, company ...): whole-value match first."""
        if not s:
            return s
        core = s.strip().strip("\x00")
        hit = self._whole.get(core.casefold() if core.isascii() else core)
        if hit is not None and len(hit) == len(core):
            if st is not None:
                st.replaced += 1
            out = s.replace(core, _case_like(core, hit), 1)
            return out
        return self.text(s, st)

    # ---- object keys ------------------------------------------------------
    def key(self, key: str) -> str:
        """Rename an object key / archive member path component by component (separators kept)."""
        return "/".join(self._component(c) for c in key.split("/"))

    def _component(self, c: str) -> str:
        if not c:
            return c
        hit = self._whole.get(c.casefold() if c.isascii() else c)
        if hit is not None and len(hit) == len(c):
            return _case_like(c, hit)
        if "__" in c:  # 'owner__repo__commit': the owner / repo parts are whole values too
            return "__".join(self._component(x) for x in c.split("__"))
        # inside a component: global rule (+ path-style sub-components, e.g. 'a\\b' in zip member names)
        # wrapped in backslashes (not "/", which the XML-tag guard would read as "</name")
        return self.passes[0].apply(("\\" + c + "\\").encode("utf-8"))[0].decode("utf-8")[1:-1]


def _case_like(orig: str, fake: str) -> str:
    if not (orig.isascii() and fake.isascii()):
        return fake
    return "".join(f.upper() if o.isupper() else f.lower() if o.islower() else f for o, f in zip(orig, fake))


@lru_cache(maxsize=1)
def default_map() -> DeidMap:
    return DeidMap.load()
