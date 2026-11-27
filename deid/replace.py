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


SOFT_MIN = 5  # keys this long may also end at a letter<->digit / CamelCase boundary
RESIDUAL_WINDOW = 8 << 20  # residual scan window (bytes); both sizes even so UTF-16 alignment is kept
RESIDUAL_OVERLAP = 1024    # longer than any key, so a hit on a window edge is seen whole in one window
SHORT_KEY = 4  # keys this short are only replaced as a whole, delimited string (never in binary noise)
_SEP = frozenset(b" \\/\"'<>:;,=_-.()[]{}|@\t\r\n")
_VALSEP = frozenset(b"\"'<>\x00\r\n\t=|;,()[]{}")  # ends of a whole field value (not space / - / _ / .)


class Replacer:
    """Aho-Corasick over the bytes (latin-1 view, 1 char == 1 byte): one automaton for case-insensitive
    ASCII keys (scanned on data.lower()), one for exact-case non-ASCII keys. Candidates are resolved
    left to right, longest first; a candidate failing its boundary / structure checks lets the next
    (shorter) candidate at that position be tried."""

    MODES = ("global", "whole", "component")

    def __init__(self, mapping: dict[str, str], mode: str = "global"):
        """mode: 'global' = word-bounded anywhere; 'whole' = only a complete delimited value (quoted string,
        XML text, length-prefixed string, line) or a path component; 'component' = only a path component
        between \\ or / separators (short user names)."""
        import ahocorasick
        assert mode in self.MODES
        self.mode = mode
        base = {k: v for k, v in mapping.items() if k and not is_generic_account(k)}
        self.dropped_generic = sorted(k for k in mapping if is_generic_account(k))
        # ASCII keys: matched case-insensitively, case pattern copied onto the pseudonym
        # (AleX / ALeX / alex ...). Non-ASCII keys: explicit case variants.
        self.mapping = {}
        for k, v in base.items():
            for ck, cv in ([(k, v)] if k.isascii() else _case_variants(k, v)):
                self.mapping.setdefault(ck, cv)
        self.originals = base
        self._length_mismatch: set[tuple[str, str]] = set()
        payload: dict[tuple[bool, bytes], list] = {}
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
                kk = kb.lower() if ci else kb
                lst = payload.setdefault((ci, kk), [])
                if not any(x[1] == enc for x in lst):
                    lst.append((kk, enc, 2 if enc == "utf-16-le" else 1, _ascii_edged(k), vb))
        self._ac = {}
        for ci in (True, False):
            A = ahocorasick.Automaton()
            n = 0
            for (c, kk), lst in payload.items():
                if c == ci:
                    A.add_word(kk.decode("latin-1"), lst)
                    n += 1
            if n:
                A.make_automaton()
                self._ac[ci] = A
        self._rules = list(self._ac)  # truthy when anything can be replaced
        self.max_key_bytes = max((len(kk) for (_, kk) in payload), default=0)
        # Short-key rule applies to ASCII keys only (random-byte collisions); CJK names are
        # typically 2-3 chars but 6-9 bytes and distinctive -> normal substring rules.
        self._nchars = {k.lower().encode(e): len(k) for k in self.mapping if k.isascii()
                        for e in _encodings_for(k) if self._enc_ok(k, e)}
        # long keys with a non-letter inside (Q.R.VELLMOR, Quorvex Bt, XQZ3DSigma): one hard edge is enough
        self._distinctive = {kk for (ci, kk) in payload if ci and len(kk.replace(b"\x00", b"")) >= 8 and
                             any(not (65 <= c <= 90 or 97 <= c <= 122) for c in kk.replace(b"\x00", b""))}

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
        if before == n + 2 and after == 0:
            return True  # NX record: 1-byte length (incl. itself and the NUL) + text + NUL
        if s >= 4 and int.from_bytes(data[s - 4:s], "little") in (n, n + 1) and (after is None or after == 0):
            return True  # OLE property set VT_LPSTR: u32 length (incl. NUL) + text + NUL
        if after is not None and after in b" _-.\\/" and (
                s >= 4 and n + 1 < int.from_bytes(data[s - 4:s], "little") < 4096 or
                before is not None and n < before < 128 and (s < 2 or data[s - 2] not in _PRINTABLE)):
            return True  # first word of a length-prefixed string (Zorb_TK_04..., ZORB 2x4 Block)
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
        if s >= 4 and data[s - 4:s - 1] == b"\xff\xfe\xff" and data[s - 1] > nch and \
                data[e + 1:e + 2] == b"\x00" and data[e:e + 1] in (b"_", b" ", b"-", b".", b"\\", b"/"):
            return True  # first word of a CString: 'Zorb_TK_04...'
        if s >= 4 and nch + 1 < int.from_bytes(data[s - 4:s], "little") < 4096 and data[e + 1:e + 2] == b"\x00" and \
                data[e:e + 1] in (b"_", b" ", b"-", b".", b"\\", b"/"):
            return True  # first word of a VT_LPWSTR

        if s >= 4 and int.from_bytes(data[s - 4:s], "little") in (nch, nch + 1) and \
                (e + 1 >= len(data) or data[e:e + 2] == b"\x00\x00"):
            return True  # OLE property set VT_LPWSTR: u32 char count (incl. NUL)
        def sep16(i):
            return 0 <= i and i + 1 < len(data) and data[i + 1] == 0 and data[i] in _SEP
        return (s < 2 or sep16(s - 2)) and (e + 1 >= len(data) or sep16(e))

    def _digit_suffix(self, data: bytes, s: int, e: int, unit: int) -> bool:
        j, n = e, 0
        while j + unit <= len(data) and 48 <= data[j] <= 57 and (unit == 1 or data[j + 1] == 0) and n < 4:
            j += unit
            n += 1
        # after the digits: a separator, or the string terminator / a control byte ('ZORB3\x00' in a record)
        if not 1 <= n <= 3 or j + unit > len(data) or unit == 2 and data[j + 1] or \
                data[j] not in b"_-. " and data[j] >= 0x20:
            return False
        # left edge: anything but a letter (record bytes before a part name are often printable: '<ZORB3_006')
        def letter(c):
            return 65 <= c <= 90 or 97 <= c <= 122
        if unit == 2:
            return s < 2 or data[s - 1] != 0 or not letter(data[s - 2])
        return s == 0 or not letter(data[s - 1])

    _LOGO_SUFFIX = b"_logo"
    _MODEL_KEY = re.compile(rb"[a-z0-9]*[0-9][a-z0-9]*")  # keys are lower-cased bytes

    def _affix(self, data: bytes, s: int, e: int, unit: int, key: bytes) -> bool:
        """Narrow glued-suffix rule (hard start required):
        - any key + '_LOGO' / '_logo' + hard end         ('ZORB_LOGO', 'zorb_logo.sym')
        - an upper-case alphanumeric product key with a digit (>= 5 chars) + ONE upper-case model letter
          + hard end                                        ('XQZ3DR\\', 'XQZ3DR v2')
        """
        if not (self._start_ok16(data, s) if unit == 2 else self._start_ok8(data, s)):
            return False
        end_ok = self._end_ok16 if unit == 2 else self._end_ok8
        lg = self._LOGO_SUFFIX if unit == 1 else self._LOGO_SUFFIX.decode().encode("utf-16-le")
        if data[e:e + len(lg)].lower() == lg and end_ok(data, e + len(lg)):
            return True
        if self._nchars.get(key, 0) >= SOFT_MIN and self._MODEL_KEY.fullmatch(key.replace(b"\x00", b"")) and e + unit <= len(data) \
                and 65 <= data[e] <= 90 and (unit == 1 or data[e + 1] == 0) and end_ok(data, e + unit):
            matched = data[s:e] if unit == 1 else data[s:e:2]
            return matched.upper() == matched  # the occurrence itself is upper-case: a product code, not a word
        return False

    def substring(self, data: bytes) -> tuple[bytes, int]:
        """Inside an opaque value (a host name like 'QUORVEXDESKTOP'): every key of >= 4 chars, no boundaries."""
        out, n, covered = bytearray(data), 0, 0
        for s, e, key, enc, unit, bounded, vb, ci in self._candidates(data):
            if s < covered or (key in self._nchars and self._nchars[key] < 4):
                continue
            out[s:e] = self._transfer_case(data[s:e], vb) if ci else vb
            n += 1
            covered = e
        return bytes(out), n

    @staticmethod
    def _soft(data: bytes, i: int, j: int, unit: int) -> bool:
        """A soft word boundary between chars i and j: letter<->digit, or CamelCase lower->Upper."""
        if i < 0 or j + unit > len(data) or unit == 2 and (data[i + 1] or data[j + 1]):
            return False
        a, b = data[i], data[j]
        isl, isd = (lambda c: 65 <= c <= 90 or 97 <= c <= 122), (lambda c: 48 <= c <= 57)
        if isl(a) and isd(b) or isd(a) and isl(b) or 97 <= a <= 122 and 65 <= b <= 90:
            return True
        # acronym running into a CamelCase word: 'XQZ3D|Paste', 'XML|Parser'
        k = j + unit
        return 65 <= a <= 90 and 65 <= b <= 90 and k < len(data) and 97 <= data[k] <= 122 and (unit == 1 or data[k + 1] == 0)

    # ---- field / path scopes -------------------------------------------
    @staticmethod
    def _component(data: bytes, s: int, e: int, unit: int) -> bool:
        """Between two path separators: C:\\Users\\<x>\\..., /home/<x>/, .../<owner>/..."""
        if unit == 2:
            return s >= 2 and data[s - 2:s] in (b"\\\x00", b"/\x00") and data[e:e + 2] in (b"\\\x00", b"/\x00")
        return s >= 1 and data[s - 1] in b"\\/" and e < len(data) and data[e] in b"\\/"

    def _whole_value(self, data: bytes, s: int, e: int, unit: int) -> bool:
        """The complete value of a field: delimited by quotes / XML text / NUL / line ends, or a length-prefixed
        string (CString, OLE VT_LPSTR / VT_LPWSTR, MFC Unicode)."""
        if unit == 2:
            nch = (e - s) // 2
            if s >= 4 and (data[s - 4:s - 1] == b"\xff\xfe\xff" and data[s - 1] == nch or
                           int.from_bytes(data[s - 4:s], "little") in (nch, nch + 1)):
                return True  # MFC Unicode CString / VT_LPWSTR

            def sep(i):
                return i < 0 or i + 1 >= len(data) or data[i + 1] == 0 and data[i] in _VALSEP
            return sep(s - 2) and sep(e)
        before = data[s - 1] if s >= 1 else None
        after = data[e] if e < len(data) else None
        n = e - s
        if before == n or before == n + 2 and after == 0 or s >= 4 and int.from_bytes(data[s - 4:s], "little") in (n, n + 1):
            return after is None or after not in _PRINTABLE or after in _VALSEP  # length-prefixed
        return (before is None or before in _VALSEP) and (after is None or after in _VALSEP)

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
        k = e
        while k < len(data) and (data[k] in _WORD or data[k] in b"_-.:"):
            k += 1
        if j >= 0 and data[j] in b"</" and (j == 0 or data[j] == ord("<") or data[j - 1] == ord("<")) and \
                (k == len(data) or data[k] in b" \t\r\n>/"):
            return True  # <Name ...> or </Name>  (not '<NAME_01.ASM\x00' in a binary record)
        return data[k:k + 2] == b'="' or data[k:k + 2] == b"='"  # attrName="..."

    # ---- public API -----------------------------------------------------
    def _candidates(self, data: bytes):
        """(start, end, matched_key_bytes, enc, unit, bounded, value, ci) for every automaton hit."""
        out = []
        for ci, A in self._ac.items():
            hay = (data.lower() if ci else data).decode("latin-1")
            for end, lst in A.iter(hay):
                for kk, enc, unit, bounded, vb in lst:
                    s = end - len(kk) + 1
                    out.append((s, end + 1, kk, enc, unit, bounded, vb, ci))
        out.sort(key=lambda c: (c[0], -(c[1] - c[0])))
        return out

    def apply(self, data: bytes) -> tuple[bytes, Stats]:
        st = Stats()
        st.skipped_length_mismatch = set(self._length_mismatch)
        if not self._rules:
            return data, st
        cands = self._candidates(data)
        if not cands:
            return data, st
        protected = [m.span() for m in _CLASS_TOKEN.finditer(data)] + [m.span() for m in _CLASS_TOKEN16.finditer(data)]
        protected.sort()
        import bisect
        pstarts = [p[0] for p in protected]
        out = bytearray(data)
        covered = 0
        for s, e, key, enc, unit, bounded, vb, ci in cands:
            if s < covered:
                continue
            i = bisect.bisect_right(pstarts, e) - 1
            hit_prot = any(protected[j][0] < e and s < protected[j][1] for j in range(max(0, i - 2), i + 1)) if protected else False
            if hit_prot or self._xml_name_context(data, s, e, unit):
                st.skipped_structural += 1
                continue
            if self.mode == "component":
                ok = self._component(data, s, e, unit)
            elif self.mode == "whole":
                ok = self._component(data, s, e, unit) or self._whole_value(data, s, e, unit)
            elif self._nchars.get(key, 99) <= SHORT_KEY:
                ok = self._whole_string16(data, s, e) if unit == 2 else self._whole_string8(data, s, e)
                if not ok:  # 'ZORB3_006_G_002': short name + up to 3 digits + separator, hard start
                    ok = self._digit_suffix(data, s, e, unit)
                if not ok:  # 'ZORB_LOGO'
                    ok = self._affix(data, s, e, unit, key)
            elif bounded:
                ok = (self._start_ok16(data, s) and self._end_ok16(data, e)) if unit == 2 else \
                     (self._start_ok8(data, s) and self._end_ok8(data, e))
                if not ok and self._nchars.get(key, 99) >= SOFT_MIN:  # 'PartsvaultStandard', 'Quorvex Bt3'
                    # one side may be a soft boundary (letter<->digit, CamelCase); the other must be hard,
                    # so identifiers like 'moQuintorUnits' (soft on both sides) are never cut into
                    hs, he = (self._start_ok16(data, s), self._end_ok16(data, e)) if unit == 2 else \
                             (self._start_ok8(data, s), self._end_ok8(data, e))
                    ok = hs and self._soft(data, e - unit, e, unit) or he and self._soft(data, s - unit, s, unit)
                    if not ok and hs:  # plural / possessive: Quorvexes, Ottos
                        for suf in (b"s", b"es", b"'s"):
                            sb = suf if unit == 1 else suf.decode().encode("utf-16-le")
                            if data[e:e + len(sb)].lower() == sb and \
                                    (self._end_ok16(data, e + len(sb)) if unit == 2 else self._end_ok8(data, e + len(sb))):
                                ok = True
                                break
                    if not ok and hs:  # 'ZORB_LOGO', product code + model letter 'XQZ3DR'
                        ok = self._affix(data, s, e, unit, key)
                    if not ok and (hs or he) and key in self._distinctive:
                        ok = True  # Q.R.VELLMOR glued to the next fixed-width field
            else:
                ok = True
            if not ok:
                st.skipped_inside_token += 1
                if len(st.samples) < 20:
                    st.samples.append(bytes(data[max(0, s - 24):e + 24]))
                continue
            out[s:e] = self._transfer_case(data[s:e], vb) if ci else vb
            st.replaced += 1
            covered = e
        return bytes(out), st

    def residual(self, data: bytes) -> list[str]:
        """Original keys still present inside real text (ASCII runs / UTF-16 runs), case-insensitive.

        Random binary (geometry, compressed payloads) is ignored; short keys must be a
        whole token. A hit means: an unknown variant / longer token containing the name ->
        caller flags the file for review or quarantine.

        Large inputs are scanned in overlapping windows: findall over a whole 75 MB XML builds
        millions of small strings at once (~9 GB peak), which OOM-killed pool workers."""
        if len(data) > RESIDUAL_WINDOW:
            hits: dict[str, None] = {}
            step = RESIDUAL_WINDOW - RESIDUAL_OVERLAP
            for i in range(0, len(data), step):
                hits.update(dict.fromkeys(self._residual_window(data[i:i + RESIDUAL_WINDOW])))
                if i + RESIDUAL_WINDOW >= len(data):
                    break
            return list(hits)
        return self._residual_window(data)

    def _residual_window(self, data: bytes) -> list[str]:
        runs = [r.decode("latin1") for r in _TEXT8.findall(data)]
        runs += [r.decode("utf-16-le", "replace") for r in _TEXT16.findall(data)]
        runs += [r.decode("utf-8", "replace") for r in _UTF8_CJK.findall(data)]
        return self.residual_text("\x00".join(runs))

    def residual_text(self, text: str) -> list[str]:
        """Original keys that start a word in `text`: the char before is not a letter, or a CamelCase
        lower->Upper transition. 'teodo' in '9900417_Teodorico' is a hit (an unknown longer name);
        'demp' in 'isRedemption' is not. Short keys must be a whole alphanumeric token."""
        if not hasattr(self, "_res_ac"):
            import ahocorasick
            self._res_ac = ahocorasick.Automaton()
            for k in self.originals:
                self._res_ac.add_word(k.lower(), k)
            if self.originals:
                self._res_ac.make_automaton()
        if not self.originals:
            return []
        low = text.lower()
        if len(low) != len(text):
            text = low
        hits = []
        for end, k in self._res_ac.iter(low):
            s = end - len(k) + 1
            if k[:1].isascii() and k[:1].isalnum():
                b = text[s - 1] if s > 0 else " "
                if b.isascii() and (b.isdigit() if len(k) <= SHORT_KEY else False):
                    continue
                if b.isascii() and b.isalpha() and not (b.islower() and text[s].isupper()):
                    continue
                if len(k) <= SHORT_KEY:
                    a = text[end + 1] if end + 1 < len(text) else " "
                    if a.isascii() and a.isalnum():
                        continue
                    # short keys in binary noise ('$OLA', '+DTU'): need a real text separator next to them
                    if b not in _TEXT_SEP and a not in _TEXT_SEP:
                        continue
            hits.append(k)
        return sorted(set(hits), key=hits.index)


_TEXT_SEP = set(" \\/_-.,'\"()")
_TEXT8 = re.compile(rb"[\x20-\x7e]{4,}")
_TEXT16 = re.compile(rb"(?:[\x20-\x7e]\x00|[\x00-\xff][\x4e-\x9f]){4,}")
_UTF8_CJK = re.compile(rb"(?:[\x20-\x7e]|[\xe0-\xef][\x80-\xbf]{2}){4,}")
