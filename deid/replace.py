"""Structure-safe entity replacement inside binary/text payloads.

Used by every native-format writer (SolidWorks 2015+ sections, OLE2 streams,
Creo, text formats). Lessons from the eDrawings test (File 10 failure):

* Never replace inside a larger identifier. A raw substring replace turned the MFC
  serialization class `moLengthUserUnits_c` into `moLengthjkztUnits_c`, which
  SolidWorks cannot resolve -> "Unhandled parsing exception".
* Binary strings carry a 1-byte length prefix right before the text, and that byte
  can be an ASCII letter (0x4A == 'J'). A naive word-boundary test would treat it
  as part of the word and silently skip (= leak) the name. A single word-byte
  sitting directly after non-printable bytes is therefore treated as a prefix.
* MFC class names (`[a-z]{2}[A-Z]..._c`) and XML tag/attribute *names* are never
  touched, whatever the mapping says.
* Longest key first, so `quorvandelibast` wins over its truncated profile-folder
  form `quorv`.
* CJK (non-ASCII-edged) keys have no word boundaries: plain substring match.
* Replacements are same-byte-length per encoding (binary formats are
  length-coupled); a key whose pseudonym differs in length in an encoding is not
  applied in that encoding and is reported.
* `residual()` finds leftover case-insensitive occurrences (e.g. a name embedded
  in an unknown longer token) so the caller can flag/quarantine instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

GENERIC_ACCOUNTS = {
    "user", "users", "admin", "administrator", "administrador", "administrateur", "owner", "guest", "default",
    "public", "all users", "pc", "test", "student", "solidworks", "autodesk", "windows", "system", "root",
    "ubuntu", "temp", "localadmin", "benutzer", "utilisateur", "usuario", "utente", "gebruiker",
}

_WORD = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
_PRINTABLE = frozenset(range(0x20, 0x7F))
_CLASS_TOKEN = re.compile(rb"[a-z]{2}[A-Z][A-Za-z0-9]*_c(?![A-Za-z0-9_])")
_CLASS_TOKEN16 = re.compile(rb"(?:[a-z]\x00){2}[A-Z]\x00(?:[A-Za-z0-9]\x00)*_\x00c\x00")


def is_generic_account(name: str) -> bool:
    return name.strip().lower() in GENERIC_ACCOUNTS


def _encodings_for(key: str) -> list[str]:
    encs = ["utf-8", "utf-16-le"]
    try:
        if key.encode("cp1252") != key.encode("utf-8"):
            encs.append("cp1252")  # e.g. 'Séba' stored ANSI in older OLE files
    except UnicodeEncodeError:
        pass
    try:
        if not key.isascii():
            encs.append("gbk")  # Chinese ANSI strings (STEP headers, old CAD)
    except UnicodeEncodeError:
        pass
    return encs


def _ascii_edged(key: str) -> bool:
    return key[:1].isascii() and key[:1].isalnum() or key[-1:].isascii() and key[-1:].isalnum()


@dataclass
class Stats:
    replaced: int = 0
    skipped_inside_token: int = 0
    skipped_structural: int = 0
    skipped_length_mismatch: set = field(default_factory=set)
    samples: list = field(default_factory=list)

    def merge(self, o: "Stats"):
        self.replaced += o.replaced
        self.skipped_inside_token += o.skipped_inside_token
        self.skipped_structural += o.skipped_structural
        self.skipped_length_mismatch |= o.skipped_length_mismatch
        self.samples += o.samples[: max(0, 20 - len(self.samples))]


def _case_variants(k: str, v: str) -> list[tuple[str, str]]:
    """k->v plus UPPER / lower / Title forms (e.g. PartsVault -> PARTSVAULT also seen)."""
    out = [(k, v)]
    for f in (str.upper, str.lower, str.title):
        fk, fv = f(k), f(v)
        if fk != k and len(fk) == len(k) and len(fv) == len(v):
            out.append((fk, fv))
    return out


SHORT_KEY = 4  # keys this short are only replaced as a whole, delimited string (never in binary noise)
_SEP = frozenset(b" \\/\"'<>:;,=_-.()[]{}|@\t\r\n")


class Replacer:
    def __init__(self, mapping: dict[str, str]):
        base = {k: v for k, v in mapping.items() if k and not is_generic_account(k)}
        self.dropped_generic = sorted(k for k in mapping if is_generic_account(k))
        # ASCII keys: matched case-insensitively, case pattern copied onto the pseudonym
        # (AleX / ALeX / alex ...). Non-ASCII keys: explicit case variants.
        self.mapping = {}
        for k, v in base.items():
            for ck, cv in ([(k, v)] if k.isascii() else _case_variants(k, v)):
                self.mapping.setdefault(ck, cv)
        self.originals = base
        self._rules = []  # (regex, table, unit, bounded, enc, case_insensitive)
        self._length_mismatch: set[tuple[str, str]] = set()
        by_enc: dict[tuple[str, bool, bool], dict[bytes, bytes]] = {}
        for k, v in self.mapping.items():
            ci = k.isascii()
            for enc in _encodings_for(k):
                try:
                    kb, vb = k.encode(enc), v.encode(enc)
                except UnicodeEncodeError:
                    continue
                if len(kb) != len(vb):
                    self._length_mismatch.add((k, enc))
                    continue
                by_enc.setdefault((enc, _ascii_edged(k), ci), {})[kb.lower() if ci else kb] = vb
        for (enc, bounded, ci), table in by_enc.items():
            keys = sorted(table, key=len, reverse=True)  # longest first
            rx = re.compile(b"|".join(re.escape(k) for k in keys), re.IGNORECASE if ci else 0)
            self._rules.append((rx, table, 2 if enc == "utf-16-le" else 1, bounded, enc, ci))
        # Short-key rule applies to ASCII keys only (random-byte collisions); CJK names are
        # typically 2-3 chars but 6-9 bytes and distinctive -> normal substring rules.
        self._nchars = {k.lower().encode(e): len(k) for k in self.mapping if k.isascii()
                        for e in _encodings_for(k) if self._enc_ok(k, e)}

    @staticmethod
    def _transfer_case(matched: bytes, value: bytes) -> bytes:
        out = bytearray(value)
        for i, c in enumerate(matched):
            if 65 <= c <= 90 and 97 <= out[i] <= 122:
                out[i] -= 32
            elif 97 <= c <= 122 and 65 <= out[i] <= 90:
                out[i] += 32
        return bytes(out)

    @staticmethod
    def _enc_ok(k: str, e: str) -> bool:
        try:
            k.encode(e)
            return True
        except UnicodeEncodeError:
            return False

    # ---- short keys: only whole, delimited strings ------------------------
    def _whole_string8(self, data: bytes, s: int, e: int) -> bool:
        n = e - s
        before = data[s - 1] if s >= 1 else None
        after = data[e] if e < len(data) else None
        if before == n and (after is None or after not in _PRINTABLE or after in _SEP):
            return True  # ANSI CString: 1-byte length prefix == key length
        if s >= 4 and int.from_bytes(data[s - 4:s], "little") in (n, n + 1) and (after is None or after == 0):
            return True  # OLE property set VT_LPSTR: u32 length (incl. NUL) + text + NUL
        return (before is None or before in _SEP) and (after is None or after in _SEP) and \
            self._printable_neighbourhood(data, s, e)

    @staticmethod
    def _printable_neighbourhood(data: bytes, s: int, e: int) -> bool:
        a, b = data[max(0, s - 3):s], data[e:e + 3]
        return all(c in _PRINTABLE for c in a + b)

    def _whole_string16(self, data: bytes, s: int, e: int) -> bool:
        nch = (e - s) // 2
        if s >= 4 and data[s - 4:s - 1] == b"\xff\xfe\xff" and data[s - 1] == nch:
            return True  # MFC Unicode CString: FF FE FF <len>
        if s >= 4 and int.from_bytes(data[s - 4:s], "little") in (nch, nch + 1) and \
                (e + 1 >= len(data) or data[e:e + 2] == b"\x00\x00"):
            return True  # OLE property set VT_LPWSTR: u32 char count (incl. NUL)
        def sep16(i):
            return 0 <= i and i + 1 < len(data) and data[i + 1] == 0 and data[i] in _SEP
        return (s < 2 or sep16(s - 2)) and (e + 1 >= len(data) or sep16(e))

    # ---- boundary logic -------------------------------------------------
    @staticmethod
    def _is_word8(data: bytes, i: int) -> bool:
        return 0 <= i < len(data) and data[i] in _WORD

    def _start_ok8(self, data: bytes, s: int) -> bool:
        if not self._is_word8(data, s - 1):
            return True
        # one word-byte directly after non-printable/start -> a length prefix, not text
        return s - 2 < 0 or data[s - 2] not in _PRINTABLE

    def _end_ok8(self, data: bytes, e: int) -> bool:
        if not self._is_word8(data, e):
            return True
        return e + 1 >= len(data) or data[e + 1] not in _PRINTABLE

    @staticmethod
    def _is_word16(data: bytes, i: int) -> bool:
        return 0 <= i and i + 1 < len(data) and data[i + 1] == 0 and data[i] in _WORD

    def _start_ok16(self, data, s):
        return not self._is_word16(data, s - 2)

    def _end_ok16(self, data, e):
        return not self._is_word16(data, e)

    @staticmethod
    def _xml_name_context(data: bytes, s: int, e: int, unit: int) -> bool:
        """True if the match is (part of) an XML tag name or attribute name."""
        if unit != 1:
            return False
        j = s - 1
        while j >= 0 and (data[j] in _WORD or data[j] in b"_-.:"):
            j -= 1
        if j >= 0 and data[j] in b"</" and (j == 0 or data[j] == ord("<") or data[j - 1] == ord("<")):
            return True  # <Name ...> or </Name>
        k = e
        while k < len(data) and (data[k] in _WORD or data[k] in b"_-.:"):
            k += 1
        return data[k:k + 2] == b'="' or data[k:k + 2] == b"='"  # attrName="..."

    # ---- public API -----------------------------------------------------
    def apply(self, data: bytes) -> tuple[bytes, Stats]:
        st = Stats()
        st.skipped_length_mismatch = set(self._length_mismatch)
        if not self._rules:
            return data, st
        protected = [m.span() for m in _CLASS_TOKEN.finditer(data)] + [m.span() for m in _CLASS_TOKEN16.finditer(data)]
        out = bytearray(data)
        for rx, table, unit, bounded, enc, ci in self._rules:
            for m in rx.finditer(data):
                s, e = m.span()
                if any(ps < e and s < pe for ps, pe in protected) or self._xml_name_context(data, s, e, unit):
                    st.skipped_structural += 1
                    continue
                key = m.group().lower() if ci else m.group()
                if self._nchars.get(key, 99) <= SHORT_KEY:
                    ok = self._whole_string16(data, s, e) if unit == 2 else self._whole_string8(data, s, e)
                elif bounded:
                    ok = (self._start_ok16(data, s) and self._end_ok16(data, e)) if unit == 2 else \
                         (self._start_ok8(data, s) and self._end_ok8(data, e))
                else:
                    ok = True
                if not ok:
                    st.skipped_inside_token += 1
                    if len(st.samples) < 20:
                        st.samples.append(bytes(data[max(0, s - 24):e + 24]))
                    continue
                out[s:e] = self._transfer_case(m.group(), table[key]) if ci else table[key]
                st.replaced += 1
        return bytes(out), st

    def residual(self, data: bytes) -> list[str]:
        """Original keys still present inside real text (ASCII runs / UTF-16 runs), case-insensitive.

        Random binary (geometry, compressed payloads) is ignored; short keys must be a
        whole token. A hit means: an unknown variant / longer token containing the name ->
        caller flags the file for review or quarantine."""
        runs = [r.decode("latin1") for r in _TEXT8.findall(data)]
        runs += [r.decode("utf-16-le", "replace") for r in _TEXT16.findall(data)]
        runs += [r.decode("utf-8", "replace") for r in _UTF8_CJK.findall(data)]
        text = "\n".join(runs).lower()
        hits = []
        for k in self.originals:
            kl = k.lower()
            if k.isascii() and len(k) <= SHORT_KEY:
                if re.search(r"(?<![a-z0-9])" + re.escape(kl) + r"(?![a-z0-9])", text):
                    hits.append(k)
            elif kl in text:
                hits.append(k)
        return hits


_TEXT8 = re.compile(rb"[\x20-\x7e]{4,}")
_TEXT16 = re.compile(rb"(?:[\x20-\x7e]\x00|[\x00-\xff][\x4e-\x9f]){4,}")
_UTF8_CJK = re.compile(rb"(?:[\x20-\x7e]|[\xe0-\xef][\x80-\xbf]{2}){4,}")
