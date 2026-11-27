"""Stage 4: map-free leak scan ("survivors").

The residual check of Stage 3 only looks for names that ARE in the map, so it cannot see a name the map is
missing. This scan uses detectors that know nothing about the map:

  email          local@domain
  user_path      C:\\Users\\<x>\\, /home/<x>/, /Users/<x>/
  host_user      HOST\\user account strings
  initials       a whole field that is 'D. Surname' / 'D.SURNAME'
  field_label    'Drawn by: X', 'Author = X', 'Gezeichnet: X' ... (the captured name)
  copyright      '(c) 2019 X', 'Copyright X'
  repo_owner     github.com/<owner>, gitlab.com/<owner>
  ner            Stage 1 NER person / org spans (all of them, also those Stage 2 dropped)

Every harvested string (binary text runs, text fields, OCR lines, path components) is rewritten with the
production map exactly as the writers do (DeidMap.text). A detector hit in the REWRITTEN string whose text
is also present in the source string survived the rewrite unchanged: a leak candidate. A pseudonym is new
text, so it never counts. Output keys are scanned directly (they are the real output).

    python -m deid.leak_scan values [--workers 14]   # -> qa/leak_scan/survivors-*.parquet
    python -m deid.leak_scan ner                     # -> qa/leak_scan/ner_survivors.parquet
    python -m deid.leak_scan keys                    # -> qa/leak_scan/key_survivors.parquet
    python -m deid.leak_scan report                  # -> qa/leak_scan/summary.json (+ per-detector top lists)
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from multiprocessing import get_context

import pyarrow as pa
import pyarrow.parquet as pq

from .config import DATA_DIR

QA_DIR = DATA_DIR / "qa" / "leak_scan"
HARVEST = DATA_DIR / "harvest"
GENERIC_USERS = {"public", "default", "default user", "all users", "administrator", "admin", "user", "users", "owner",
                 "guest", "test", "temp", "localservice", "networkservice", "systemprofile", "runner", "appveyor",
                 "vagrant", "ubuntu", "root", "pi", "student", "jenkins", "build", "circleci", "docker"}
ROLE_MAIL = re.compile(r"(?i)^(no-?reply|info|support|contact|sales|admin|webmaster|office|hello|help|service|team|"
                       r"press|mail|postmaster|abuse|security|privacy|legal|billing|orders?|jobs|careers|example|test)$")
DETECTORS = {
    "email": re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]{2,64}@[A-Za-z0-9-]{2,63}(?:\.[A-Za-z0-9-]{2,63})+)"),
    "user_path": re.compile(r"(?i)(?:[a-z]:[\\/](?:users|documents and settings|dokumente und einstellungen)[\\/]"
                            r"|(?<![\w.])/(?:home|Users)/)([^\\/:*?\"<>|\x00-\x1f]{2,40})[\\/]"),
    "host_user": re.compile(r"(?<![\w\\])([A-Za-z][A-Za-z0-9-]{2,30})\\([A-Za-z][A-Za-z0-9._-]{1,30})(?![\w\\])"),
    "initials": re.compile(r"^\s*((?:[A-Z]\. ?){1,2}[A-Z][a-z]{2,}(?:-[A-Z][a-z]+)?|(?:[A-Z]\. ?){1,2}[A-Z]{3,})\s*$"),
    "field_label": re.compile(r"(?i)\b(?:drawn|checked|approved|designed|engineer|author|created|modified|zeichner|"
                              r"gezeichnet|gepr[uü]ft|bearbeiter|konstrukt\w*|disegnato|controllato|dessin[ée]|"
                              r"v[ée]rifi[ée]|dibujado|revisado|autor)(?:\s+by)?\s*[:=]\s*"
                              r"((?-i:[A-Z][\w'’.-]+)(?:\s(?-i:[A-Z][\w'’.-]+)){0,2})"),
    "copyright": re.compile(r"(?:[Cc]opyright|\([cC]\)|©)\s*(?:\(c\)\s*)?(?:\d{4}(?:\s*[-–,]\s*\d{2,4})*\s*,?\s*)?(?:by\s+)?"
                            r"((?:[A-Z][\w&'’.-]+)(?:\s(?:[A-Z][\w&'’.-]+|&|and|of|de|von|van)){0,5})"),
    "repo_owner": re.compile(r"(?i)\b(?:github|gitlab)\.com[/:]([A-Za-z0-9][A-Za-z0-9_.-]{1,38})"),
}
COPYRIGHT_KEEP = re.compile(r"(?i)^(the|all|this|copyright|notice|holder|owner|year|by|free software|python software|"
                            r"apache|linux|creative commons|open cascade|microsoft|adobe|autodesk|dassault|ptc|siemens|"
                            r"spatial|font|bitstream|google|mozilla|unicode|ieee|iso|x consortium|massachusetts institute)")
_dm = None


def _init():
    global _dm
    from .rewrite.mapping import DeidMap
    _dm = DeidMap.load()


def detect(value: str) -> list[tuple[str, str]]:
    """(detector, matched text) for every map-free detector hit in `value`."""
    out = []
    for name, rx in DETECTORS.items():
        for m in rx.finditer(value):
            s = m.group(1).strip()
            if len(s) < 3:
                continue
            if name == "email" and ROLE_MAIL.match(s.split("@")[0]):
                continue
            if name in ("user_path",) and s.lower() in GENERIC_USERS:
                continue
            if name == "host_user" and (s.split("\\")[0].lower() in GENERIC_USERS or not any(c.isalpha() for c in s)):
                continue
            if name == "copyright" and COPYRIGHT_KEEP.match(s):
                continue
            out.append((name, s if name != "host_user" else m.group(0)))
    return out


def survivors(value: str, dm=None) -> list[tuple[str, str]]:
    """Detector hits that survive the production rewrite of `value` unchanged."""
    dm = dm or _dm
    new = dm.text(value)
    hits = detect(new)
    return [(d, s) for d, s in hits if s in value and s in new]


def _chunk(values: list[str]) -> list[dict]:
    rows = []
    for v in values:
        try:
            for d, s in survivors(v):
                rows.append(dict(value=v[:2000], detector=d, match=s))
        except Exception:  # noqa: BLE001 - odd binary text
            continue
    return rows


SCHEMA = pa.schema([("value", pa.string()), ("detector", pa.string()), ("match", pa.string())])


def run_values(workers: int) -> None:
    import duckdb
    QA_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    # cheap prefilter: only strings that could trigger a detector at all
    pre = r"@|\\|/home/|/Users/|[Cc]opyright|\([cC]\)|©|github\.com|gitlab\.com|^\s*[A-Z]\. ?[A-Za-z]|(?i)(drawn|checked|approved|designed|engineer|author|created|modified|zeichner|gezeichnet|gepr|bearbeiter|konstrukt|disegnato|controllato|dessin|rifi|dibujado|revisado|autor)"
    rel = con.execute(f"""SELECT DISTINCT value FROM (
            SELECT value FROM read_parquet('{HARVEST}/occurrences.parquet') UNION ALL
            SELECT value FROM read_parquet('{HARVEST}/ocr_occurrences.parquet') UNION ALL
            SELECT value FROM read_parquet('{HARVEST}/paths.parquet'))
        WHERE length(value) < 4000 AND regexp_matches(value, ?)""", [pre])
    part, total = 0, 0
    with get_context("fork").Pool(workers, initializer=_init) as pool:
        while True:
            batch = [v for (v,) in rel.fetchmany(400_000)]
            if not batch:
                break
            total += len(batch)
            chunks = [batch[i:i + 2000] for i in range(0, len(batch), 2000)]
            rows = [r for part_rows in pool.imap_unordered(_chunk, chunks) for r in part_rows]
            pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), QA_DIR / f"survivors-{part:04d}.parquet")
            part += 1
            print(f"{total} candidate strings scanned, {len(rows)} survivor hits in this batch", flush=True)


def run_ner() -> None:
    """Stage 1 NER spans (person / org, any score >= 0.5) that survive the rewrite of their source piece."""
    import duckdb
    from .entities import is_kept
    from .ner_run import NER_DIR
    from .rewrite.mapping import DeidMap
    dm = DeidMap.load()
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    rows = con.execute(f"""SELECT s.text, s.label, max(s.score) sc, i.text piece, max(i.nb) nb, list_sort(list(DISTINCT k))[1:4] kinds
        FROM read_parquet('{NER_DIR}/spans-*.parquet') s JOIN read_parquet('{NER_DIR}/input.parquet') i USING (tid), unnest(i.kinds) t(k)
        WHERE s.score >= 0.5 GROUP BY s.text, s.label, i.text""").fetchall()
    agg: dict = {}
    for text, label, sc, piece, nb, kinds in rows:
        t = text.strip()
        if len(t) < 3 or is_kept(t):
            continue
        new = dm.text(piece)
        if not re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", new):
            continue
        a = agg.setdefault(t, dict(match=t, labels=Counter(), score=0.0, nb=0, kinds=set(), example=piece[:300]))
        a["labels"][label] += 1
        a["score"] = max(a["score"], sc)
        a["nb"] += nb or 0
        a["kinds"].update(kinds)
    QA_DIR.mkdir(parents=True, exist_ok=True)
    out = [dict(match=a["match"], label=a["labels"].most_common(1)[0][0], score=round(a["score"], 3), nb=a["nb"],
                kinds=",".join(sorted(a["kinds"])), example=a["example"]) for a in agg.values()]
    pq.write_table(pa.Table.from_pylist(out), QA_DIR / "ner_survivors.parquet")
    print(dict(ner_survivors=len(out), by_label=Counter(r["label"] for r in out).most_common(8)))


def run_keys() -> None:
    """Output object keys (the real output): detectors per path component."""
    import duckdb
    from .rewrite_run import RW_DIR
    con = duckdb.connect()
    rows = []
    for (k,) in con.execute(f"SELECT DISTINCT new_key FROM read_parquet('{RW_DIR}/keys.parquet')").fetchall():
        for comp in k.split("/"):
            for d, s in detect(comp):
                rows.append(dict(key=k, component=comp, detector=d, match=s))
    QA_DIR.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows or [dict(key="", component="", detector="", match="")]), QA_DIR / "key_survivors.parquet")
    print(dict(key_hits=len(rows), by_detector=Counter(r["detector"] for r in rows)))


def report() -> None:
    import duckdb
    from .rewrite_run import RW_DIR
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute(f"CREATE VIEW sv AS SELECT * FROM read_parquet('{QA_DIR}/survivors-*.parquet')")
    con.execute(f"""CREATE TABLE okb AS SELECT sha256 FROM read_parquet('{RW_DIR}/parts/status-*.parquet', filename=true)
        QUALIFY row_number() OVER (PARTITION BY sha256 ORDER BY status='error', filename DESC)=1 AND status IN ('ok','copy')""")
    con.execute(f"""CREATE TABLE occ AS SELECT DISTINCT o.sha256, o.value FROM (
        SELECT sha256, value FROM read_parquet('{HARVEST}/occurrences.parquet') UNION ALL
        SELECT sha256, value FROM read_parquet('{HARVEST}/ocr_occurrences.parquet')) o
        SEMI JOIN (SELECT DISTINCT value FROM sv) USING (value) SEMI JOIN okb USING (sha256)""")
    top = {}
    for (d,) in con.execute("SELECT DISTINCT detector FROM sv").fetchall():
        top[d] = [dict(match=m, files=f, values=v) for m, f, v in con.execute(
            """SELECT sv.match, count(DISTINCT occ.sha256) f, count(DISTINCT sv.value) v FROM sv LEFT JOIN occ USING (value)
               WHERE detector=? GROUP BY 1 ORDER BY 2 DESC, 3 DESC""", [d]).fetchall()]
    summ = {d: dict(distinct=len(v), files=con.execute(
        "SELECT count(DISTINCT occ.sha256) FROM sv JOIN occ USING (value) WHERE detector=?", [d]).fetchone()[0])
        for d, v in top.items()}
    json.dump(dict(summary=summ, top={d: v[:300] for d, v in top.items()}), open(QA_DIR / "summary.json", "w"),
              ensure_ascii=False, indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["values", "ner", "keys", "report"])
    ap.add_argument("--workers", type=int, default=14)
    a = ap.parse_args()
    {"values": lambda: run_values(a.workers), "ner": run_ner, "keys": run_keys, "report": report}[a.cmd]()
