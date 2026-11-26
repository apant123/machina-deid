"""Deterministic, length-preserving pseudonyms (Stage 2).

Every pseudonym is generated *per token* from HMAC(seed, class|token), so one real token maps to
one fake token everywhere ("Vallemo" in "Teodorico_Vallemo", "VALLEMO" in a title block and
"Vallemo" alone all become the same surname). Surfaces are rendered by substituting tokens and
keeping separators, generic organisation words ("University", "GmbH", "de") and digits' positions.

Length rules (binary CAD formats and renamed object keys are length-coupled, see deid.replace):
* same number of characters, and per character the same UTF-8 width; a cp1252-encodable
  accented letter is replaced by a cp1252-encodable accented letter (same width in cp1252 too);
* CJK characters are replaced by common GB2312 level-1 characters (3 bytes UTF-8, 2 GBK, 2 UTF-16);
* case pattern is copied from the original (ALEX -> MIRA, alex -> mira, AleX -> MirA).

The seed lives in `.env` (DEID_SEED, owner-only) and in the private staging bucket - never in code.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import random
import re
import secrets
import unicodedata
from functools import lru_cache

from .config import _DOTENV

VOWELS, CONS = "aeiou", "bcdfghklmnprstvz"
CJK_RANGE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def seed() -> bytes:
    s = os.environ.get("DEID_SEED")
    if not s:
        s = secrets.token_hex(32)
        with open(_DOTENV, "a") as f:
            f.write(f"\nDEID_SEED={s}\n")
        os.chmod(_DOTENV, 0o600)
        os.environ["DEID_SEED"] = s
    return bytes.fromhex(s)


def _rng(*parts: str) -> random.Random:
    h = hmac.new(seed(), "|".join(parts).encode("utf-8"), hashlib.sha256).digest()
    return random.Random(int.from_bytes(h, "big"))


# ---------------------------------------------------------------- pools
@lru_cache(maxsize=1)
def name_pools() -> tuple[dict, dict]:
    """first/last names by length (ASCII only), from Faker locales."""
    import warnings
    warnings.filterwarnings("ignore")
    from faker import Faker
    locs = ["en_US", "en_GB", "de_DE", "fr_FR", "es_ES", "es_MX", "it_IT", "pt_BR", "pt_PT", "nl_NL", "da_DK",
            "sv_SE", "no_NO", "pl_PL", "cs_CZ", "fi_FI", "en_IE", "en_AU", "en_CA"]
    first, last = set(), set()
    for loc in locs:
        p = next(x for x in Faker(loc).providers if "person" in type(x).__module__)
        for a in ("first_names", "first_names_male", "first_names_female"):
            v = getattr(p, a, None)
            first |= set(v.keys() if isinstance(v, dict) else v or [])
        v = getattr(p, "last_names", None)
        last |= set(v.keys() if isinstance(v, dict) else v or [])
    ok = re.compile(r"^[A-Z][a-z]+$")
    by = lambda s: {n: sorted(x for x in s if ok.match(x) and len(x) == n) for n in range(2, 16)}
    return by(first), by(last)


@lru_cache(maxsize=1)
def cjk_pools() -> tuple[list[str], list[str], list[str]]:
    """(surname chars, common chars) - GB2312 level 1 only (same byte widths in UTF-8/GBK/UTF-16)."""
    common = []
    for hi in range(0xB0, 0xD8):
        for lo in range(0xA1, 0xFF):
            try:
                common.append(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    surnames = list("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾萧田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤")
    given = list("伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华建文辉玲红晶亮成林斌宇浩凯鹏飞俊峰雪琳欣怡佳琪婷雅思宁博涵晨轩然子豪睿哲铭昊天瑞泽楠鑫")
    return surnames, [c for c in given if c in common] or common, common


_ACCENTED = "áàâäãåéèêëíìîïóòôöõúùûüñçýÿāēīōūǎěǐǒǔǖǘǚǜąęćłńśźżőűčďěňřšťžůœæøß"


def _profile(c: str) -> tuple:
    """Byte width of c in every encoding the Replacer may use (None = not encodable)."""
    out = []
    for enc in ("utf-8", "utf-16-le", "cp1252", "gbk"):
        try:
            out.append(len(c.encode(enc)))
        except UnicodeEncodeError:
            out.append(None)
    return tuple(out)


@lru_cache(maxsize=None)
def _accent_cands(prof: tuple) -> list[str]:
    return [c for c in _ACCENTED if _profile(c) == prof]


def _shape(orig: str, fake: str, r: random.Random) -> str:
    """Copy case pattern and per-char byte widths of `orig` onto ASCII `fake` (same length)."""
    out = []
    for o, f in zip(orig, fake):
        if o.isdigit():
            f = str(r.randrange(10)) if o.isascii() else o
        elif "Ѐ" <= o <= "ӿ" or "Ͱ" <= o <= "Ͽ":  # Cyrillic / Greek: same-script letters (same widths everywhere)
            pool = "абвгдежзиклмнопрстуфхэюя" if o >= "Ѐ" else "αβγδεζηθικλμνξοπρστυφχψω"
            f = pool[(ord(f.lower()) * 7 + r.randrange(len(pool))) % len(pool)]
            f = f.upper() if o.isupper() else f
        elif not o.isascii() and o.isalpha():
            cands = _accent_cands(_profile(o.lower()))
            base = f.lower()
            same = [c for c in cands if unicodedata.normalize("NFKD", c)[0] == base]
            f = r.choice(same or cands) if cands else o  # no equivalent -> keep char (lengths never change)
        elif not o.isalnum() or not o.isascii():
            f = o  # punctuation, and non-ASCII digits/symbols ('¾'): kept as-is
        out.append(f.upper() if o.isupper() and f.upper() != f and len(f.upper()) == 1 and _profile(f.upper()) == _profile(o) else f)
    s = "".join(out)
    assert len(s) == len(orig)
    return s


def synthetic_word(n: int, r: random.Random) -> str:
    """Pronounceable lowercase word of exactly n letters: strict consonant/vowel alternation with
    the occasional two-letter cluster (\"tr\", \"nd\") - reads like a name, never like noise."""
    clusters = ["tr", "nd", "st", "br", "ll", "rk", "nt", "ch", "sh", "rn", "lv", "gr"]
    s, v = "", r.random() < 0.25
    while len(s) < n:
        if v:
            s += r.choice(VOWELS)
        elif s and len(s) + 2 <= n - 1 and r.random() < 0.2:
            s += r.choice(clusters)
        else:
            s += r.choice(CONS)
        v = not v
    return s[:n]


def fake_token(tok: str, cls: str, avoid: frozenset = frozenset()) -> str:
    """One token -> fake token of identical length/shape. cls: given|surname|word|acronym|digits|cjk_person|cjk."""
    key = tok.casefold()
    for attempt in range(50):
        r = _rng(cls, key, str(attempt))
        if CJK_RANGE.search(tok):
            sur, given, common = cjk_pools()
            out = []
            for i, ch in enumerate(tok):
                if not CJK_RANGE.match(ch):
                    out.append(ch)
                elif "一" <= ch <= "鿿":
                    pool = (sur if i == 0 else given) if cls == "cjk_person" else common
                    out.append(r.choice(pool))
                elif "぀" <= ch <= "ゟ":
                    out.append(chr(r.randrange(0x3042, 0x3094)))  # hiragana
                elif "゠" <= ch <= "ヿ":
                    out.append(chr(r.randrange(0x30A2, 0x30F4)))  # katakana
                else:
                    out.append(chr(r.randrange(0xAC00, 0xD7A4)))  # hangul syllable
            fake = "".join(out)
        elif tok.isdigit():
            fake = str(r.randrange(1, 10)) + "".join(str(r.randrange(10)) for _ in tok[1:]) if len(tok) > 1 else str(r.randrange(10))
        else:
            n = len(tok)
            first, last = name_pools()
            pool = (first if cls == "given" else last if cls == "surname" else {}).get(n) or []
            if cls == "acronym":
                base = "".join(r.choice("BCDFGHJKLMNPRSTVWZ" if i % 2 == 0 else "AEIOUBCDFGHKLMNPRSTV") for i in range(n))
            elif pool and all(c.isalpha() for c in tok):
                base = r.choice(pool)  # accented originals: pool name, accents re-applied by _shape
            else:
                base = synthetic_word(n, r)
            fake = _shape(tok, base, r)
        if fake.casefold() != key and fake.casefold() not in avoid:
            return fake
    return fake  # pragma: no cover


def same_widths(a: str, b: str) -> bool:
    if len(a) != len(b) or len(a.encode()) != len(b.encode()) or len(a.encode("utf-16-le")) != len(b.encode("utf-16-le")):
        return False
    for enc in ("cp1252", "gbk"):
        try:
            ea = a.encode(enc)
        except UnicodeEncodeError:
            continue
        try:
            if len(b.encode(enc)) != len(ea):
                return False
        except UnicodeEncodeError:
            return False
    return True
