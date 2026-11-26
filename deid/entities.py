"""Stage 2b: entity resolution -> global pseudonym map.

Sources (strongest evidence first):
  owner   repo owners from object keys (every one is an account/org)
  field   author / company / last-saved-by / USER / SITE ... metadata values, parsed
          (HOST\\user, e-mails, URLs, phones, copyright lines)
  regex   e-mails (real TLDs), profile-folder user names (C:\\Users\\x, /home/x ...), URL hosts,
          phone numbers - over *all* 45 M values, including binary text runs
  ner     GLiNER spans over human text (deid.ner_run), accepted by score x shape x dictionary rules
  vendor  the public supplier / catalogue gazetteer (config/entity_policy.yaml)

Kept (never replaced): CAD/software tool names, standards, infrastructure domains, generic accounts.

Each map row is a *surface* string -> pseudonym, with
  scope  'global' = word-bounded match anywhere (deid.replace.Replacer)
         'field'  = only as a complete metadata field value or a complete path component; used for weak
                    single tokens (dictionary words "space"/"Infinity", <= 3 chars, numeric ids)
Outputs (PII - private data dir + private staging bucket only, never the code bucket):
  ~/data/machina/entities/{entities,map,decisions}.parquet

    python -m deid.entities build
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .config import DATA_DIR, ROOT
from .harvest_run import HARVEST_DIR
from .ner_run import NER_DIR
from .pseudonym import CJK_RANGE, fake_token, same_widths
from .replace import GENERIC_ACCOUNTS

ENT_DIR = DATA_DIR / "entities"
POLICY = ROOT / "config" / "entity_policy.yaml"

# ------------------------------------------------------------------ vocab
ORG_WORDS = set("""university universidad universidade università universita universität universitat université universite
universiteit uniwersytet univerzita universitet college colegio institute institut instituto istituto school escuela
escola schule hochschule fachhochschule polytechnic politecnico polytechnique politécnica politecnica tecnológico
tecnologico laboratory laboratories laboratorio laboratoire lab labs center centre centro research robotics robotic
technologies technology tech technical technische technik tecnologia engineering engineers ingenieria ingeniería
industries industrial industrie systems system solutions software hardware electronics electronic elektronik devices
device design designs studio studios works workshop foundation fundación fundacion stiftung group grupo gruppe team
club society sociedad association asociación community consortium initiative project projects open source machines
machine machinery motors motor manufacturing supply supplies company compañía corporation corp co inc incorporated
ltd limited llc llp gmbh ag kg sa sas sarl sl srl spa bv nv ab as asa oy pty pte plc sdn bhd kft sro ug ohg mbh
of de del della di du des la le el the and und et y e für for at in science sciences ciencias applied angewandte
national nacional federal state polytechnic department dept faculty facultad medical health aerospace space energy
dynamics automation mechanical mechanics mechatronics scientific international global digital innovation innovations
products product services service consulting enterprises enterprise holding holdings partners trading
""".split())
CJK_ORG_WORDS = ["有限责任公司", "股份有限公司", "有限公司", "公司", "大学", "学院", "研究院", "研究所", "实验室", "科技", "技术",
                 "机器人", "集团", "工作室", "智能", "中心", "工程", "委员会", "学会", "公社", "高校", "局"]
# generic CJK words that NER labels as orgs once an org word is stripped ('机械学院', '下水道局', '市公社')
CJK_GENERIC = set("""机械 机械工程 下水道 水道 都市整備 政策企画 市 工業 工程 电气 电子 自动化 材料 计算机 设计 信息 建筑 土木 交通
化学 物理 数学 能源 环境 管理 经济 艺术 国际 中国 日本 大二上 大一 大二 大三 大四""".split())
CODE_SRC = re.compile(r"#define|::|->|\b0x[0-9A-Fa-f]+|[;{}]|\w\(\s*\w+\s*\)|&\w|<<|>>|@\w+_\w+|@(?:brief|defgroup|addtogroup|param|arg|retval|ref|note|ingroup)\b")


@lru_cache(maxsize=None)
def cjk_surnames() -> frozenset:
    from faker import Faker
    from .pseudonym import cjk_pools
    out = set(cjk_pools()[0])
    for loc in ("zh_CN", "zh_TW", "ja_JP"):
        out.update(n[0] for n in Faker(loc).provider("faker.providers.person").last_names)
    return frozenset(out)


@lru_cache(maxsize=200_000)
def cjk_zipf(w: str) -> float:
    import logging
    import jieba
    jieba.setLogLevel(logging.ERROR)
    from wordfreq import zipf_frequency
    return zipf_frequency(w, "zh")


@lru_cache(maxsize=200_000)
def en_zipf(w: str) -> float:
    from wordfreq import zipf_frequency
    return zipf_frequency(w, "en")
NAME_PARTICLES = {"de", "da", "do", "dos", "das", "del", "della", "di", "van", "von", "der", "den", "le", "la", "du",
                  "bin", "ibn", "y", "e", "al", "el", "ten", "ter", "st",
                  "mr", "mrs", "ms", "dr", "prof", "ing", "sr", "jr", "sir", "herr", "frau"}
ORG_SUFFIX = re.compile(r"(?i)\b(inc|ltd|llc|gmbh|corp|corporation|company|co\.|s\.?a\.?|s\.?l\.?|ag|b\.?v\.?|pte|pty|plc|"
                        r"srl|spa|oy|ab|univ\w*|institut\w*|college|laborator\w*|labs?|foundation|technolog\w*|"
                        r"robotics|industries|systems|electronics|engineering|dynamics|design|designs|studio|studios|works|research|motors|"
                        r"aerospace|automation|solutions|devices|instruments|manufacturing|machines|academy|school|district|"
                        r"consortium|initiative|foundation|society|association|group|team|club|hub)\b|大学|公司|学院|研究所|实验室")

PERSON_FIELDS = re.compile(r"(file_name\.author|meta\.(author|last_saved_by)|prop\.(lastModifiedBy|LastModifiedBy|CreatedBy|"
                           r"SW-Author|SW-Author\(Author\)|Author|Designer|creator|CREATOR|ProfileUserName|Owner)$|"
                           r"global\.author|\$AUTHOR|header\.USER|dsc\.For|^creo\.user|xmp\.prop\.(creator|Author)$|"
                           r"xmp\.attr\.(dc:creator|xap:Author|pdf:Author)|prop\..*_person$|attr\.(userName|author)$)")
ORG_FIELDS = re.compile(r"(file_name\.org|meta\.company|prop\.(Company|CompanyName|Copyright)$|global\.org|header\.SITE|"
                        r"dsc\.Copyright|prism:copyright|prop\..*_organization$)")

TLDS = set("""com org net edu gov mil int info biz io ai co me tech dev app xyz online site eu asia name pro museum aero
coop jobs mobi tel travel cloud design studio systems network solutions science engineering technology academy
education institute university works tools""".split()) | set(
    """ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt bw by bz ca cc cd
    cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg er es et eu fi fj fk fm fo fr ga gd ge gf
    gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km
    kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz
    na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg
    sh si sk sl sm sn so sr ss st sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug uk us uy uz va vc ve
    vg vi vn vu wf ws ye yt za zm zw""".split())
SUBDOMAIN_KEEP = {"www", "www2", "ww1", "ww2", "mail", "web", "en", "de", "fr", "es", "it", "uk", "us", "cdn", "docs",
                  "doc", "wiki", "forum", "forums", "blog", "shop", "store", "support", "help", "api", "static", "assets",
                  "downloads", "download", "dl", "files", "resources", "datasheet", "datasheets", "tools", "my", "m",
                  "go", "info", "news", "dev", "edu", "ac", "co", "com", "org", "net", "gov"}

EMAIL_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,12}")
PHONE_RX = re.compile(r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?\(?\d{2,4}\)?[ .-]\d{3,4}[ .-]\d{3,4}(?![\w.])")
HOSTUSER_RX = re.compile(r"^([A-Za-z0-9][\w.-]{0,30})\\([^\\()]{1,40}?)(?:\s*\(([^)]*)\))?$")
TOOL_DESCRIPTORS = set("""step iges stl dxf dwg pdf translator translation converter export exporter import importer
version build release edition professional premium standard student educational education academic research
x64 x86 dc cs cc sp1 sp2 sp3 sp4 sp5 bit plugin plug addin add in sp service pack update rev beta alpha preview personal pro plus ultimate mcad ecad
library libraries toolkit kernel sdk api for windows linux mac macos distiller writer printer driver dll exe office
word excel powerpoint visio cloud desktop online web server client viewer reader studio suite
corp corporation inc incorporated ltd gmbh llc co sa ag company systemes systèmes syst""".split())
PROFILE_SPECIAL = {"public", "default", "default user", "all users", "shared", "desktop", "documents", "appdata", "home",
                   "administrator", "admin", "user", "users", "guest", "temp", "tmp", "local", "localservice",
                   "networkservice", "systemprofile", "defaultapppool", "runner", "ubuntu", "root", "pi", "vagrant"}
TOKEN_RX = re.compile(r"[぀-ヿ㐀-鿿가-힯]+|[^\W\d_]+|\d+", re.UNICODE)


@lru_cache(maxsize=1)
@lru_cache(maxsize=1)
def policy() -> dict:
    """Public lists from config/entity_policy.yaml, plus the vendor gazetteer, which lives in the private data dir:
    every vendor in it is replaced, so its names must not ship in the code bucket."""
    p = yaml.safe_load(POLICY.read_text())
    vf = DATA_DIR / "entities" / "vendors.yaml"
    p["vendors"] = (yaml.safe_load(vf.read_text()) or []) if vf.exists() else []
    return p


class _Dictionary:
    """'Is this an ordinary word?' - GCIDE headwords (+ plural / -es / -ies forms) or a frequent English
    word (wordfreq zipf >= 3.5) that is not a personal name in the Faker pools ('demo' yes, 'zephyn' no)."""

    def __init__(self):
        from english_words import get_english_words_set
        from .pseudonym import name_pools
        self.words = frozenset(get_english_words_set(["gcide"], lower=True, alpha=True))
        self.names = frozenset(n.lower() for pools in name_pools() for p in pools.values() for n in p)

    @lru_cache(maxsize=500_000)
    def __contains__(self, w: str) -> bool:
        w = w.lower()
        if w in self.words or w.endswith("s") and w[:-1] in self.words or w.endswith("es") and w[:-2] in self.words \
                or w.endswith("ies") and w[:-3] + "y" in self.words:
            return True
        if w in self.names or len(w) < 3:
            return False
        from wordfreq import zipf_frequency
        return zipf_frequency(w, "en") >= 3.5 or max(zipf_frequency(w, lg) for lg in ("fr", "es", "de", "it", "pt")) >= 3.3


@lru_cache(maxsize=1)
def dictionary() -> _Dictionary:
    return _Dictionary()


def key(s: str) -> str:
    """Canonical entity key: accents stripped, casefolded, separators removed (CJK kept)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", s.casefold())


@lru_cache(maxsize=1)
def keep_keys() -> frozenset:
    p = policy()
    ks = {key(x) for x in p["keep_tools"] + p["keep_terms"] + p["generic_accounts"]} | {key(x) for x in GENERIC_ACCOUNTS}
    return frozenset(k for k in ks if k)


def is_kept(s: str) -> bool:
    k = key(s)
    if not k or k in keep_keys():
        return True
    # tool stamp with a version ("SOLIDWORKS 2018 SP4.0", "Open CASCADE STEP translator 7.4")
    # ... i.e. the value *starts* with a kept tool name (token prefix)
    # and everything after it is version / product descriptor ("2018 SP4.0", "STEP translator 7.4")
    toks = TOKEN_RX.findall(s)
    for i in range(1, len(toks) + 1):
        if key(" ".join(toks[:i])) in keep_keys():
            rest = toks[i:]
            if all(t.isdigit() or t.lower() in TOOL_DESCRIPTORS or key(t) in keep_keys()
                   for t in rest):
                return True
    return False


def host_kept(host: str) -> bool:
    host = host.lower().strip(".")
    return any(host == d or host.endswith("." + d) for d in policy()["keep_domains"])


@dataclass
class Ent:
    k: str
    etype: Counter = field(default_factory=Counter)
    surfaces: Counter = field(default_factory=Counter)
    sources: set = field(default_factory=set)
    evidence: list = field(default_factory=list)
    nb: int = 0
    alias_of: str | None = None
    tags: set = field(default_factory=set)
    xlink: str | None = None  # cross-script twin (白鹤 <-> BaiHeBot)

    @property
    def type(self) -> str:
        if self.etype["person"] and any(ORG_SUFFIX.search(s) or any(t.lower() in ORG_WORDS - NAME_PARTICLES
                                                                     for t in TOKEN_RX.findall(s)) for s in self.surfaces):
            return "org"  # 'GRAB Lab' tagged person by NER / field heuristics
        for t in ("email", "domain", "phone", "person", "org", "vendor", "brand", "account"):
            if self.etype[t] and self.etype[t] >= max(self.etype.values()) / 3:
                return t
        return self.etype.most_common(1)[0][0]


class Builder:
    def __init__(self):
        self.ents: dict[str, Ent] = {}
        self.decisions: list[dict] = []  # dropped / kept candidates, for the report

    def add(self, surface: str, etype: str, source: str, nb: int = 1, evidence: str = "", tag: str | None = None):
        surface = re.sub(r"\s+", " ", surface).strip(" \t\"'.,;:()[]{}<>|")
        if len(surface) < 2 or not re.search(r"[^\W\d_]", surface) or "${" in surface or \
                re.search(r"[\x00-\x1f\x7f-\x9f\ufffd\uffff]", surface):
            return
        if is_kept(surface):
            self.decisions.append(dict(surface=surface, etype=etype, source=source, decision="keep", reason="policy", nb=nb))
            return
        k = key(surface)
        if len(k) < 2:
            return
        e = self.ents.setdefault(k, Ent(k))
        e.etype[etype] += 1
        e.surfaces[surface] += nb
        e.sources.add(source)
        e.tags.add(tag or source)
        e.nb = max(e.nb, nb)
        if evidence and len(e.evidence) < 3:
            e.evidence.append(evidence[:160])

    def drop(self, surface, etype, source, reason, nb=1, score=None):
        self.decisions.append(dict(surface=surface, etype=etype, source=source, decision="drop", reason=reason, nb=nb,
                                   score=score))

    # -------------------------------------------------------------- sources
    def from_owners(self):
        for r in pq.read_table(HARVEST_DIR / "paths.parquet").to_pylist():
            if r["kind"] == "path.owner" and r["value"].lower() not in ("gitlab.com", "github.com"):
                self.add(r["value"], "account", "owner", r["count"], "repo owner")
                # distinctive parts of org-style owners: 'ZORBLAT' (ZORBLAT-Inc), 'QUVEX', 'VELK' (VELK-Robotique)
                parts = re.split(r"[-_.]+", r["value"])
                if len(parts) == 2 and parts[0].lower() in self.first_names() and parts[1].isalpha():
                    self.add(f"{parts[0]} {parts[1]}", "person", "owner", r["count"], f"repo owner {r['value']}", tag="owner")
                elif len(parts) >= 2:
                    dic = dictionary()
                    for t in parts:
                        if len(t) >= 4 and t.isalpha() and t.lower() not in dic and t.lower() not in ORG_WORDS \
                                and en_zipf(t) < 2.5 \
                                and not is_kept(t) and t.lower() not in self.first_names():
                            self.add(t, "org", "owner", r["count"], f"part of repo owner {r['value']}", tag="owner")

    def _field_value(self, v: str, kind: str, nb: int, org: bool):
        v = re.split(r"[\x00-\x1f\ufffd\uffff]", v)[0].strip()  # OLE property junk after the string
        ev = f"{kind}={v}"
        if org:
            v = re.sub(r"(?i)(©|\(c\)|copyright|all rights reserved\.?|\b(19|20)\d\d\b)", " ", v).strip(" ,.-")
        for m in EMAIL_RX.finditer(v):
            self.email(m.group(), nb, "field")
        v = EMAIL_RX.sub(" ", v)
        for m in re.finditer(r"https?://[^\s,;]+", v):
            self.url(m.group(), nb, "field")
        v = re.sub(r"https?://[^\s,;]+", " ", v)
        for m in PHONE_RX.finditer(v):
            self.add(m.group(), "phone", "field", nb, ev)
        v = PHONE_RX.sub(" ", v)
        if m := HOSTUSER_RX.match(v.strip()):
            host, user = m.group(1), m.group(2).strip()
            self.add(user, "account", "field", nb, ev, tag="hostuser")
            if not re.fullmatch(r"(?i)(desktop|laptop|pc|win|workstation|user)-?\w{0,8}", host) and \
                    len(host) > 3 and host.lower() not in dictionary():
                self.add(host, "account", "field", nb, ev)  # machine names often embed the person/org
            self.add(f"{host}\\{user}", "account", "field", nb, ev)
            return
        for part in (re.split(r"\s*[;|]\s*", v) if org else re.split(r"\s*[,;|]\s*|\s+-\s+", v)):
            part = part.strip()
            if not part:
                continue
            toks = [t for t in TOKEN_RX.findall(part) if not t.isdigit()]
            if not toks:
                continue
            if org:
                if len(key(part)) >= 3:
                    acronym = bool(re.fullmatch(r"[A-Z][A-Z0-9&]{2,5}", part))
                    self.add(part, "org", "field", nb, ev, tag="org_acronym" if acronym else "field")
            elif CJK_RANGE.search(part):
                self.add(part, "person", "field", nb, ev)
            elif len(toks) >= 2 and all(t[:1].isupper() for t in toks if t.lower() not in NAME_PARTICLES):
                orgish = ORG_SUFFIX.search(part) or any(t.lower() in ORG_WORDS - NAME_PARTICLES for t in toks)
                dic = dictionary()
                if not orgish and all(t.lower() in dic for t in toks) and not any(t.lower() in dic.names for t in toks):
                    self.drop(part, "person", "field", "all dictionary words (e.g. 'Roller Bearings')", nb)
                    continue
                self.add(part, "org" if orgish else "person", "field", nb, ev)
            else:
                self.add(part, "org" if ORG_SUFFIX.search(part) else "account", "field", nb, ev)

    def from_fields(self):
        import duckdb
        rows = duckdb.sql(f"""SELECT kind, value, n_blobs FROM read_parquet('{HARVEST_DIR}/strings.parquet')
            WHERE regexp_matches(kind, '{PERSON_FIELDS.pattern.replace("'", "''")}')
               OR regexp_matches(kind, '{ORG_FIELDS.pattern.replace("'", "''")}')
               OR (kind LIKE '%image.meta' AND value LIKE 'vti_author:%')""").fetchall()
        for kind, v, nb in rows:
            if v.startswith("vti_author:"):
                v = v.split("|", 1)[-1]
            if len(v) > 200:
                continue
            self._field_value(v, kind, nb, org=bool(ORG_FIELDS.search(kind)))

    def email(self, e: str, nb: int, source: str):
        local, _, dom = e.rpartition("@")
        labels = dom.lower().split(".")
        if labels[-1] not in TLDS or len(labels) < 2 or not re.search(r"[a-z]", local, re.I) or len(local) < 2 or \
                len(labels[-2]) < 2 or not (dom.islower() or dom.isupper()) or len(dom) < 5:
            return False  # binary noise such as 'Zu@bpV.LT', 'if@p.ki'
        self.add(e, "email", source, nb, "email")
        if len(local) >= 4 and local.lower() not in dictionary() and not re.fullmatch(
                r"(?i)(info|contact|support|sales|admin|office|mail|hello|noreply|no-reply|webmaster)", local):
            self.add(local, "account", source, nb, f"email local part of {e}", tag="email_local")
        if dom.lower() not in policy()["generic_mail_domains"]:
            self.domain(dom, nb, source)
        return True

    def url(self, u: str, nb: int, source: str):
        m = re.match(r"(?i)(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(/[^\s]*)?", u)
        if not m:
            return
        host, path = m.group(1), m.group(2) or ""
        if host.lower() in ("github.com", "gitlab.com", "www.github.com") and path:
            seg = path.strip("/").split("/")[0]
            if seg and seg.lower() not in dictionary() and len(seg) >= 3 and \
                    seg.lower() not in ("orgs", "users", "topics", "search", "features", "about", "login"):
                self.add(seg, "account", source, nb, f"{host}{path[:60]}", tag="url_owner")
            return
        self.domain(host, nb, source)

    @staticmethod
    def registrable(host: str) -> str | None:
        """'pricing.partsnoop.com' -> 'partsnoop.com', 'mek.xku.dk' -> 'mek.xku.dk', 'www.voltshop.co.uk' -> 'voltshop.co.uk'.
        Distinctive sub-labels are kept in the surface (mek.xku.dk); generic ones (www, pricing, docs) are not."""
        labels = host.lower().strip(".").split(".")
        if len(labels) < 2 or labels[-1] not in TLDS:
            return None
        n_suffix = 2 if len(labels) >= 3 and labels[-2] in ("co", "com", "ac", "edu", "org", "net", "gov", "or", "ne") else 1
        core = labels[:-n_suffix]
        while len(core) > 1 and (core[0] in SUBDOMAIN_KEEP or core[0].isdigit() or len(core[0]) < 2 or
                                 core[0] in ("pricing", "repos", "scripts", "focus", "cam1", "katalog", "motioncontrolsystems")):
            core = core[1:]
        return ".".join(core + labels[-n_suffix:]) if core and core[-1] not in SUBDOMAIN_KEEP else None

    def domain(self, host: str, nb: int, source: str):
        host = host.lower().strip(".")
        if host_kept(host) or host in policy()["generic_mail_domains"]:
            return
        reg = self.registrable(host)
        if reg and not host_kept(reg) and reg not in policy()["generic_mail_domains"]:  # outlook.live.com
            self.add(reg, "domain", source, nb, f"host {host}")

    def from_regex(self):
        import duckdb
        con = duckdb.connect()
        con.execute("SET memory_limit='12GB'")
        vals = f"read_parquet('{HARVEST_DIR}/values.parquet')"
        for e, nb in con.execute(rf"""SELECT e, sum(n_blobs) FROM (SELECT unnest(regexp_extract_all(value,
                '{EMAIL_RX.pattern}')) e, n_blobs FROM {vals} WHERE contains(value, '@')) GROUP BY 1""").fetchall():
            self.email(e, nb, "regex")
        # drive-rooted Windows profiles, or /home|/Users at the start of a Unix path (no free-floating matches)
        prof = (r"(?:(?:^|[^A-Za-z0-9])[A-Za-z]:[\\/](?:Users|USERS|users|Documents and Settings|DOCUME~1|Dokumente und Einstellungen|"
                r"Usuarios|Utilisateurs|Gebruikers|Utenti|Benutzer)[\\/]|(?:^|[\s\"'=(:,])/(?:home|Users)/)"
                r"([A-Za-z0-9][A-Za-z0-9._ ~-]{0,39}|[^\x00-\x7f\\/:*?\"<>|]{1,20})[\\/]")
        for u, nb in con.execute(f"""SELECT u, sum(n_blobs) FROM (SELECT unnest(regexp_extract_all(value, '{prof.replace("'", "''")}', 1)) u,
                n_blobs FROM {vals} WHERE regexp_matches(value, '(?i)users|settings|/home/|einstellungen|usuarios|utilisateurs|gebruikers|utenti|benutzer'))
                GROUP BY 1""").fetchall():
            u = u.strip()
            if "~" in u:
                self.short_names.append((u, nb))  # 8.3 names, linked to their long form below
            elif re.search(r"[^\W\d_]", u) or re.fullmatch(r"\d{4,}", u):
                if u.lower() not in PROFILE_SPECIAL:
                    toks = u.split()
                    person = len(toks) >= 2 and all(t[:1].isupper() and t.isalpha() for t in toks)
                    self.add(u, "person" if person else "account", "regex", nb, "profile folder", tag="profile")
            elif u.isdigit():
                self.add(u, "account", "regex", nb, "profile folder (id)", tag="profile")
        for u, nb in con.execute(rf"""SELECT u, sum(n_blobs) FROM (SELECT unnest(regexp_extract_all(value,
                '(?i)(?:https?://|www\.)[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[A-Za-z0-9_.~-]+)?')) u, n_blobs FROM {vals}
                WHERE regexp_matches(value, '(?i)https?://|www\.')) GROUP BY 1""").fetchall():
            self.url(u, nb, "regex")
        # OneDrive - <Org> sync roots
        for o, nb in con.execute(r"""SELECT o, sum(n_blobs) FROM (SELECT unnest(regexp_extract_all(value,
                'OneDrive - ([^\\/]{2,60})[\\/]', 1)) o, n_blobs FROM """ + vals + """ WHERE contains(value, 'OneDrive - '))
                GROUP BY 1""").fetchall():
            self.add(o, "org", "regex", nb, "OneDrive tenant")
        # phones in human text only (field values handled above)
        for v, nb in con.execute(rf"""SELECT value, n_blobs FROM read_parquet('{HARVEST_DIR}/strings.parquet')
                WHERE regexp_matches(kind, '^(pdf\.text|svg\.text|text\.(txt|md|html\.text)|dxf\.c1|dwg\.c1|ocr)')
                  AND regexp_matches(value, '(?i)(tel|phone|fax|mobile|mob|cell|☎)[.:\s]')""").fetchall():
            for m in PHONE_RX.finditer(v):
                if len(re.sub(r"\D", "", m.group())) >= 9:
                    self.add(m.group(), "phone", "regex", nb, v)

    def from_vendors(self):
        for v in policy()["vendors"]:
            self.add(v, "vendor", "vendor", 1, "gazetteer")

    def from_manual(self):
        """Reviewer additions after the spot-check: ENT_DIR/manual_seeds.yaml (PII -> data dir, never in code)."""
        f = ENT_DIR / "manual_seeds.yaml"
        for r in (yaml.safe_load(f.read_text()) or []) if f.exists() else []:
            self.add(r["surface"], r.get("type", "org"), "manual", 0, "manual seed (spot-check review)", tag="owner")

    def from_ner(self):
        import duckdb
        dic = dictionary()
        rows = duckdb.sql(f"""
            SELECT s.text, s.label, max(s.score) mx, avg(s.score) av, count(DISTINCT s.tid) nt, sum(i.nb) nb,
                   list(DISTINCT k)[1:6] kinds, any_value(i.text) ex
            FROM read_parquet('{NER_DIR}/spans-*.parquet') s JOIN read_parquet('{NER_DIR}/input.parquet') i USING (tid),
                 unnest(i.kinds) t(k)
            GROUP BY 1, 2""").fetchall()
        best: dict[str, list] = {}
        for text, label, mx, av, nt, nb, kinds, ex in rows:
            k = key(text)
            b = best.get(k)
            if b is None or mx > b[2]:
                best[k] = [text, label, mx, av, nt, nb, kinds, ex]
            else:
                b[4] += nt
                b[5] += nb
        for text, label, mx, av, nt, nb, kinds, ex in best.values():
            ok, why, etype = self.accept_span(text, label, mx, nt, dic, ex)
            if ok:
                self.add(text, etype, "ner", nb, f"{label}:{mx:.2f} '{ex[:100]}'")
            else:
                self.drop(text, label, "ner", why, nb, mx)

    @staticmethod
    def accept_span(text: str, label: str, score: float, nt: int, dic, src: str | None = None) -> tuple[bool, str, str]:
        t = text.strip()
        while True:  # 'Gabriela de' -> 'Gabriela'
            parts = t.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].lower() in NAME_PARTICLES:
                t = parts[0]
            else:
                break
        etype = {"person": "person", "brand": "brand"}.get(label, "org")
        if src is not None:
            letters = sum(c.isalpha() or c.isspace() for c in src)
            if letters < 0.6 * len(src) and len(src) > len(t) + 3:
                return False, "source is not text (binary / code)", etype
        if len(t) < 2 or len(t) > 60:
            return False, "length", etype
        if "_" in t and any(len(x) >= 2 and x.isupper() for x in t.split("_")):
            return False, "code identifier ('DMA_Channel', 'PWR_Flag')", etype
        if src is not None and CODE_SRC.search(src):
            return False, "source looks like code / register map ('#define', '::', '0x..', ';')", etype
        toks = [x for x in TOKEN_RX.findall(t) if not x.isdigit()]
        if not toks:
            return False, "no letters", etype
        if is_kept(t):
            return False, "policy keep", etype
        if any(sum(c.isdigit() for c in x) >= 3 for x in re.split(r"[\s_-]+", t)) or sum(c.isdigit() for c in t) * 2 >= len(t):
            return False, "part number / code", etype
        cjk = bool(CJK_RANGE.search(t))
        if cjk:
            if all(ch in "".join(CJK_ORG_WORDS) for ch in re.sub(r"[^一-鿿]", "", t)):
                return False, "cjk generic org word", etype
            if label == "person":  # must start with a surname; job titles / words ('工程师', '专家') are frequent
                ok = score >= 0.7 and 2 <= len(t) <= 4 and t[0] in cjk_surnames() and cjk_zipf(t) < 3.5
                return ok, "cjk person: no surname / common word / score", etype
            org_w = next((w for w in CJK_ORG_WORDS if t.endswith(w) and len(t) > len(w)), "")
            rest = t[:-len(org_w)] if org_w else t
            if rest in CJK_GENERIC or cjk_zipf(rest) >= 3.0 or len(rest) < 2:
                return False, "cjk org: generic remainder ('机械学院', '下水道局')", etype
            return (score >= 0.6 and org_w or score >= 0.9 and len(t) >= 3), "cjk org", etype
        # CamelCase parts count as words too ('PinceArriere' = pince + arriere)
        words = [p.lower() for x in toks for p in (re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", x) or [x])
                 if p.lower() not in NAME_PARTICLES]
        caps = all(x[:1].isupper() for x in toks if x.lower() not in NAME_PARTICLES and x.lower() not in ORG_WORDS)
        nondict = [w for w in words if w not in dic and w not in ORG_WORDS and len(w) >= 3]
        if label == "person":
            sp = [x for x in re.split(r"[\s.,]+", t) if x and x.lower() not in NAME_PARTICLES]
            if all(len(x.strip("-'")) <= 2 for x in sp):
                return False, "person: initials only ('D. S', 'Q-Li')", etype
            if len(sp) == 1 and len(words) >= 2:
                return False, "person: CamelCase single token ('WFei', 'KimH')", etype
            if all(x.isupper() for x in toks) and all(len(x) <= 3 for x in toks):
                return False, "person: short all-caps tokens ('HAL TIM')", etype
            if len(words) >= 2 and caps:
                if nondict and score >= 0.6 or score >= 0.85:
                    return True, "", etype
                return False, "person: all dictionary words", etype
            if len(words) == 1 and caps and nondict and score >= 0.85 and len(words[0]) >= 4:
                return True, "", etype
            return False, "person: single token / low score / not capitalised", etype
        if ORG_SUFFIX.search(t) and score >= 0.5 and (nondict or len(words) >= 2 and caps and score >= 0.7):
            return True, "", etype
        if len(toks) >= 2 and all(x.isupper() for x in toks) and score < 0.9:
            return False, "all-caps code / abbreviation chain ('HDWR-HYDR-MASTFIT')", etype
        if label == "university" and len(toks) == 1 and (score < 0.95 or not re.fullmatch(r"[A-Z][a-z]+", toks[0])):
            return False, "single-token 'university' (GLiNER labels unknown codes so)", etype
        if not nondict:
            return False, "org/brand: only dictionary words", etype
        if len(toks) > 1 and all(len(x) <= 3 for x in toks):
            return False, "org/brand: only short tokens", etype
        if len(toks) == 1:
            w = toks[0]
            if w.isupper():  # 'USART', 'FSMC', 'PRESCALER': NER alone is not enough for an all-caps token
                return False, "single all-caps token (needs non-NER evidence)", etype
            if len(w) <= 5:  # acronyms: binary-ish strings yield 'UFYn' / 'KSII' 'universities' -> need context
                ok = w.isupper() and score >= 0.75 and len(src or "") >= len(t) + 10
                return ok, "short acronym without context", etype
            parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", w)
            if len(parts) > 1 and (any(p_.lower() in dic for p_ in parts) or len(parts) >= 3):
                return False, "CamelCase identifier ('BaseRoulante', 'ThdSchPat')", etype
            if score >= 0.75 and w[:1].isupper():
                return True, "", etype
            return False, "org/brand single token: score/shape", etype
        if score >= 0.6 and caps:
            return True, "", etype
        return False, "org/brand: low score / not capitalised", etype

    # -------------------------------------------------------------- linking
    @staticmethod
    def weak(k: str, surface: str) -> bool:
        """Single dictionary word, <= 3 chars, or digits only: never safe as a free-text key."""
        toks = [t for t in TOKEN_RX.findall(surface) if not t.isdigit()]
        if CJK_RANGE.search(surface):
            return len(surface) <= 1
        return k.isdigit() or len(toks) <= 1 and (len(k) <= 3 or bool(toks) and toks[0].lower() in dictionary())

    def prune(self):
        """Weak single tokens need strong evidence: a profile folder / repo owner / HOST\\user / URL owner,
        or two independent sources. 'Toolbox', 'Name', 'CAD' as author values are dropped here."""
        strong = {"owner", "profile", "hostuser", "url_owner", "org_acronym"}
        for k, e in list(self.ents.items()):
            if e.type in ("email", "domain", "phone"):
                continue
            main = e.surfaces.most_common(1)[0][0]
            if not self.weak(k, main):
                continue
            if "vendor" in e.tags and len(k) >= 3 or e.tags & strong or len(e.tags - {"vendor"}) >= 2:
                continue
            for srf, n in e.surfaces.items():
                self.drop(srf, e.type, ",".join(sorted(e.tags)), "weak single token without corroboration", n)
            del self.ents[k]

    def link_aliases(self):
        """Truncated profile names ('quorv' -> 'quorvandelibast') and 8.3 names ('TENHOR~1')."""
        longs = [e for e in self.ents.values() if e.type in ("account", "person") and len(e.k) >= 7]
        for e in list(self.ents.values()):
            # Windows truncates Microsoft-account profile folders to 5 chars ('quorv' for quorvandelibast)
            if e.type != "account" or len(e.k) != 5 or e.k in self.first_names() or \
                    not all(x.isascii() for x in e.surfaces) or "regex" not in e.sources and "field" not in e.sources:
                continue
            cands = sorted((x for x in longs if x.k != e.k and x.k.startswith(e.k)), key=lambda x: len(x.k))
            # unique, or a prefix chain (quorv < quorvan < quorvandelibast) -> the longest
            if cands and all(cands[-1].k.startswith(c.k) for c in cands):
                e.alias_of = cands[-1].k
        for u, nb in self.short_names:
            base = key(u.split("~")[0])
            if len(base) < 3 or is_kept(u.split("~")[0]) or any(key(g).startswith(base) for g in GENERIC_ACCOUNTS | set(policy()["generic_accounts"])):
                self.decisions.append(dict(surface=u, etype="account", source="regex", decision="keep", reason="8.3 of generic", nb=nb))
                continue
            cands = [x for x in self.ents.values() if x.k.startswith(base) and x.type in ("account", "person")]
            if cands:
                tgt = max(cands, key=lambda x: x.nb)
                self.add(u, "account", "regex", nb, f"8.3 name of {tgt.k}")
                self.ents[key(u)].alias_of = tgt.k
            else:
                self.add(u, "account", "regex", nb, "8.3 profile folder")

    def link_scripts(self):
        """Link CJK entities to Latin ones by pinyin ('白鹤' -> baihe ~ 'BaiHeBot'); report-only link,
        each script keeps its own same-width pseudonym (a CJK string cannot share bytes with a Latin one)."""
        from pypinyin import lazy_pinyin
        norm = lambda x: re.sub(r"([aeiou])\1+", r"\1", x)
        latin = {}
        for e in self.ents.values():
            if not CJK_RANGE.search(e.k):
                latin.setdefault(norm(e.k), e.k)
        for e in self.ents.values():
            if CJK_RANGE.search(e.k) and e.type != "person":
                py = norm("".join(lazy_pinyin(re.sub(r"[^一-鿿]", "", e.k))))
                if len(py) >= 5 and py in latin:
                    e.xlink = latin[py]
                    self.ents[latin[py]].xlink = e.k

    # -------------------------------------------------------------- pseudonyms
    @staticmethod
    @lru_cache(maxsize=1)
    def first_names() -> frozenset:
        from .pseudonym import name_pools
        return frozenset(n.casefold() for pool in name_pools()[0].values() for n in pool)

    def render_domain(self, host: str, token_map: dict) -> str:
        labels = host.split(".")
        n_suffix = 2 if len(labels) >= 3 and labels[-2].lower() in ("co", "com", "ac", "edu", "org", "net", "gov", "or", "ne") else 1
        out = []
        for i, lab in enumerate(labels):
            if i >= len(labels) - n_suffix or lab.lower() in SUBDOMAIN_KEEP:
                out.append(lab)
            else:
                out.append(re.sub(r"[^\W\d_]+|\d+", lambda m: self._tok(m.group(), "digits" if m.group().isdigit() else
                                  "acronym" if len(m.group()) <= 4 else "word", token_map), lab))
        return ".".join(out)

    HANDLE_PATTERNS = (lambda g, s: s + g[0], lambda g, s: g[0] + s, lambda g, s: g + s, lambda g, s: s + g,
                       lambda g, s: g + s[0], lambda g, s: g + "." + s, lambda g, s: g + "_" + s, lambda g, s: g[0] + "." + s)

    def build_handle_index(self, token_map: dict):
        """'talvenkm' = Talvenk+M(ene), 'orvank' = Orvan+K(estro): handle -> derived from the person's fake tokens."""
        self.handle_index = {}
        for e in self.ents.values():
            if e.type != "person":
                continue
            for srf in e.surfaces:
                toks = [t for t in TOKEN_RX.findall(srf) if t.isalpha() and t.isascii()]
                if len(toks) != 2:
                    continue
                g, sn = toks[0].lower(), toks[1].lower()
                fg, fs = token_map.get(("name", g)), token_map.get(("name", sn))
                if not fg or not fs:
                    continue
                for pat in self.HANDLE_PATTERNS:
                    h = pat(g, sn)
                    if len(h) >= 5:
                        self.handle_index.setdefault(h, pat(fg.lower(), fs.lower()))

    def render(self, surface: str, e: Ent, token_map: dict) -> str:
        """Substitute distinctive tokens; keep separators, generic org words, particles, TLDs."""
        etype = e.type
        h = getattr(self, "handle_index", {}).get(surface.lower())
        if h and etype == "account" and len(h) == len(surface):
            return "".join(f.upper() if o.isupper() else f for o, f in zip(surface, h))
        if "@" in surface and etype in ("email", "account"):
            local, _, dom = surface.rpartition("@")
            keep_dom = dom.lower() in policy()["generic_mail_domains"] or host_kept(dom)
            return self.render(local, Ent(key(local), Counter(account=1)), token_map) + "@" + \
                (dom if keep_dom else self.render_domain(dom, token_map))
        if etype == "domain":
            return self.render_domain(surface, token_map)
        out, pos, idx = [], 0, 0
        toks = list(TOKEN_RX.finditer(surface))
        word_toks = [m for m in toks if not m.group().isdigit()]
        for m in toks:
            out.append(surface[pos:m.start()])
            tok = m.group()
            low = tok.lower()
            if tok.isdigit():
                cls = "digits" if etype == "phone" or len(word_toks) == 0 else None  # ids / phones only
            elif CJK_RANGE.search(tok):
                cls = "cjk_person" if etype == "person" else "cjk"
                suffix = next((w for w in CJK_ORG_WORDS if etype != "person" and tok.endswith(w) and len(tok) > len(w)), "")
                if suffix:  # keep generic org suffix characters (有限公司, 大学 ...)
                    out.append(self._tok(tok[:-len(suffix)], cls, token_map) + suffix)
                    pos = m.end()
                    idx += 1
                    continue
            elif etype in ("org", "vendor", "brand") and low in ORG_WORDS and len(word_toks) > 1:
                cls = None
            elif low in NAME_PARTICLES and etype == "person" and len(word_toks) > 1:
                cls = None
            elif etype == "person":
                cls = "given" if (idx == 0 and len(word_toks) > 1 or len(word_toks) == 1 and low in self.first_names()) \
                    else "surname"
            elif etype == "account" and len(word_toks) == 1 and low in self.first_names():
                cls = "given"  # 'Zephyn', 'Corvalo' as account names -> a real-looking first name
            elif len(tok) <= 4:
                cls = "acronym"  # same class for every non-person type -> 'XQZ' identical in XQZ3D / XQZ3DSigma
            else:
                cls = "word"
            if not tok.isdigit():
                idx += 1
            out.append(self._tok(tok, cls, token_map) if cls else tok)
            pos = m.end()
        out.append(surface[pos:])
        res = "".join(out)
        if res == surface and word_toks and not getattr(self, "_forcing", False) and etype not in ("domain",):
            self._forcing = True  # every token was generic -> pseudonymise them all
            try:
                res = self.render(surface, Ent(e.k, Counter(account=1)), token_map) if len(word_toks) > 1 else res
            finally:
                self._forcing = False
        return res

    def _tok(self, tok: str, cls: str, token_map: dict) -> str:
        if cls == "digits":
            return fake_token(tok, "digits")
        # one fake per (token, family): given/surname share 'name', everything else 'word'
        fam = "name" if cls in ("given", "surname") else cls
        cf = tok.casefold()
        k = (fam, cf if len(cf) == len(tok) else tok.lower())  # 'Weßels' must not share 'wessels' (other length)
        if k not in token_map:
            token_map[k] = fake_token(tok, cls, self.real_tokens)
        fake = token_map[k]
        # token_map stores a case-folded-equivalent fake; re-shape to this occurrence's case
        out = "".join(f.upper() if o.isupper() else f.lower() if o.islower() else f for o, f in zip(tok, fake))
        return out if len(out) == len(tok) else fake

    def build_map(self) -> list[dict]:
        self.real_tokens = frozenset(t.casefold() for e in self.ents.values() for s in e.surfaces for t in TOKEN_RX.findall(s))
        token_map: dict = {}
        rows = []
        dic = dictionary()
        # persons first (their tokens seed handle derivation), aliases last
        order = sorted(self.ents.values(), key=lambda e: (e.alias_of is not None, e.type != "person", -e.nb))
        self.handle_index = {}
        indexed = False
        fakes: dict[str, str] = {}  # entity -> pseudonym of its main surface
        def root(k):
            seen = set()
            while self.ents.get(k) and self.ents[k].alias_of and k not in seen:
                seen.add(k)
                k = self.ents[k].alias_of
            return k
        for e in order:
            if not indexed and e.type != "person":
                self.build_handle_index(token_map)
                indexed = True
            if e.alias_of:
                r = root(e.k)
                tk = key(fakes[r]) if r in fakes else ""
                for s in e.surfaces:
                    base = re.match(r"[^~.]*", s).group()  # 'TENHOR' of 'TENHOR~1', 'quorv'
                    rest = s[len(base):]
                    if len(tk) >= len(base) and base.isascii():
                        fb = "".join(f.upper() if o.isupper() else f.lower() for o, f in zip(base, tk))
                    else:
                        fb = self.render(base, e, token_map)
                    rows.append(self._row(e, s, fb + rest, dic))  # '~1' and the 8.3 extension are kept
                    fakes.setdefault(e.k, fb + rest)
                continue
            for i, (s, fake) in enumerate(self.variants(e, token_map)):
                if i == 0:
                    fakes[e.k] = fake
                rows.append(self._row(e, s, fake, dic))
        return rows

    def _row(self, e: Ent, s: str, fake: str, dic) -> dict:
        toks = [t.lower() for t in TOKEN_RX.findall(s) if not t.isdigit()]
        k = key(s)
        scope = "field" if e.type not in ("phone", "email", "domain") and self.weak(k, s) else "global"
        if scope == "field" and (e.type == "vendor" and len(k) >= 4 or "org_acronym" in e.tags and s.isupper() and len(k) >= 3):
            scope = "global"  # deliberate gazetteer vendors (Lego, igus) / org acronyms (DTU): Replacer still
            #                   only replaces keys <= 4 chars as whole delimited strings
        if scope == "field" and len(k) <= 3 and not CJK_RANGE.search(s):
            scope = "path"  # profile-folder / repo-owner component only
        return dict(surface=s, pseudonym=fake, entity=e.k, etype=e.type, scope=scope,
                    sources=",".join(sorted(e.sources)), nb=e.nb, observed=s in e.surfaces,
                    alias_of=e.alias_of, xlink=e.xlink, evidence=" | ".join(e.evidence), widths_ok=same_widths(s, fake))

    def variants(self, e: Ent, token_map: dict) -> list[tuple[str, str]]:
        """(surface, pseudonym): observed surfaces (most frequent first) + separator / order variants of
        multi-token names, derived from the SAME token pseudonyms (kept only if the scan sees them)."""
        out: dict[str, str] = {}
        for s, _ in e.surfaces.most_common():
            out.setdefault(s, self.render(s, e, token_map))
        dic = dictionary()
        for s, f in list(out.items()):
            toks, ftoks = s.split(" "), f.split(" ")
            if len(toks) < 2 or len(toks) != len(ftoks) or CJK_RANGE.search(s) or e.type not in ("person", "org", "vendor", "brand"):
                continue
            for sep in ("_", "-", ".", ""):
                out.setdefault(sep.join(toks), sep.join(ftoks))
            if e.type == "person" and len(toks) == 2 and toks[0][:1].isalpha() and ftoks[0][:1].isalpha():
                out.setdefault(f"{toks[1]}, {toks[0]}", f"{ftoks[1]}, {ftoks[0]}")
                out.setdefault(f"{toks[1]} {toks[0]}", f"{ftoks[1]} {ftoks[0]}")
                if len(toks[0].rstrip(".")) >= 2 and len(toks[1]) >= 4:  # not from 'Zelnik O' -> 'NO'
                    out.setdefault(f"{toks[0][0]}. {toks[1]}", f"{ftoks[0][0]}. {ftoks[1]}")
                    out.setdefault(f"{toks[0][0]}{toks[1]}", f"{ftoks[0][0]}{ftoks[1]}")  # jsmith-style handle
            # rare surname tokens alone ("Kestro", "Talvenk"): non-dictionary, >= 5 letters, and the person is
            # corroborated beyond NER ('Jack Ploder' from NER must not turn every 'Ploder' into a surname)
            if e.type == "person" and set(e.sources) - {"ner"}:  # ... and not a kept term ('Allen' key, 'Phillips')
                for t, ft in zip(toks[1:], ftoks[1:]):
                    if len(t) >= 5 and t.isalpha() and t.lower() not in dic and t.lower() not in NAME_PARTICLES \
                            and not is_kept(t):
                        out.setdefault(t, ft)
        return [(s, f) for s, f in out.items()
                if s in e.surfaces or CJK_RANGE.search(s) or len(s) >= 4]

    def run(self):
        self.short_names: list = []
        print("owners", flush=True)
        self.from_owners()
        print("fields", flush=True)
        self.from_fields()
        print("regex", flush=True)
        self.from_regex()
        print("vendors", flush=True)
        self.from_vendors()
        self.from_manual()
        print("ner", flush=True)
        if list(NER_DIR.glob("spans-*.parquet")):
            self.from_ner()
        self.prune()
        self.link_aliases()
        self.link_scripts()
        rows = self.build_map()
        ENT_DIR.mkdir(parents=True, exist_ok=True)
        # token-level map rows from different entities may share a surface: first (highest-evidence) wins
        seen, uniq = set(), []
        for r in rows:  # ASCII surfaces are matched case-insensitively -> one row per casefold
            sk = r["surface"].casefold() if r["surface"].isascii() else r["surface"]
            if sk not in seen:
                seen.add(sk)
                uniq.append(r)
        pq.write_table(pa.Table.from_pylist(uniq), ENT_DIR / "map_candidates.parquet")
        pq.write_table(pa.Table.from_pylist([dict(entity=e.k, type=e.type, types=dict(e.etype).__repr__(),
                                                  surfaces=list(e.surfaces), sources=sorted(e.sources), nb=e.nb,
                                                  alias_of=e.alias_of, xlink=e.xlink, evidence=e.evidence)
                                             for e in self.ents.values()]),
                       ENT_DIR / "entities.parquet")
        pq.write_table(pa.Table.from_pylist(self.decisions), ENT_DIR / "decisions.parquet")
        print(dict(entities=len(self.ents), map_rows=len(uniq), types=Counter(e.type for e in self.ents.values()),
                   sources=Counter(s for e in self.ents.values() for s in e.sources),
                   decisions=Counter((d["decision"], d["reason"]) for d in self.decisions).most_common(12)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    a = ap.parse_args()
    Builder().run()
    sys.exit(0)
