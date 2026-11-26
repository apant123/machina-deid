"""Stage 2a: multilingual NER over the human-text strings of the harvest (local CPU, nothing leaves the box).

Input = unique *text-like* values (metadata, PDF/SVG/DXF text, labels, STEP product names, source
comments, file/dir names, OCR), de-duplicated by digit template ("Part12" ~ "Part13") so each shape
is run once. Binary text runs (SolidWorks/Creo/OLE/exe runs) are NOT run through NER: they are
noisy and huge; they are covered by dictionary propagation of the entities found here plus regex.

    python -m deid.ner_run input            # -> harvest/ner/input.parquet
    python -m deid.ner_run pieces           # prefilter (candidate proper-noun token / CJK / @) + split
    python -m deid.ner_run run --workers 7  # NER_THREADS=2; resumable parts -> harvest/ner/spans-*.parquet
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from multiprocessing import get_context

import pyarrow as pa
import pyarrow.parquet as pq

from .harvest_run import HARVEST_DIR

NER_DIR = HARVEST_DIR / "ner"
MODEL = "urchade/gliner_multi-v2.1"  # Apache-2.0, multilingual (incl. zh/ja/de/es/fr/pt)
LABELS = ["person", "company", "organization", "university", "brand"]
THRESHOLD = 0.35  # recall-oriented; Stage 2b scores spans by label score x frequency x context

# Kinds that are geometry/noise, binary runs, timestamps or ids -> excluded from NER.
EXCLUDE_KINDS = (r"(\.run|^bin\.|^sw\.raw|^f3z\.raw|^text\.(g[a-z0-9]{1,3}|arc|unv|l\d+|pho|gr1|pff|gdd|ipc|cmp|map|list"
                 r"|dat|drl|xln|nc|ngc|gcode|tap|pos|csv|asc|pts|xyz|ply|s19|hex|ihex|log)$|svg\.attr\.(id|d|transform"
                 r"|style|points)|\.attr\.(p:UUID|uuid|id|swTransform)|\.prop\.(SW-Last Saved Date|SW-Created Date|created"
                 r"|modified)|creationDate|modDate|sw\.section|json\.geojson|sat\.token|xt\.token)")
# Must contain something name-like: a capitalised word, an all-caps word, CJK/kana/hangul, or '@'.
NAMEISH = r"[A-Z][a-z]|[A-Z]{3,}|[぀-鿿가-힯]|@"
SCHEMA = pa.schema([("tid", pa.int64()), ("text", pa.string()), ("label", pa.string()), ("score", pa.float64()),
                    ("start", pa.int64()), ("end", pa.int64())])


def build_input() -> None:
    import duckdb
    NER_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{HARVEST_DIR / 'duck_tmp'}'")
    norm = (r"""trim(regexp_replace(regexp_replace(value, '[_\s]+', ' ', 'g'),
            '\.(step|stp|sldprt|sldasm|slddrw|stl|pdf|igs|iges|dwg|dxf|png|jpe?g|prt|asm|drw|ipt|iam|fcstd|3mf|f3d|obj|zip|md|txt)$', '', 'i'))""")
    con.execute(f"""CREATE TABLE src AS
        SELECT value, n_blobs AS nb, kind FROM read_parquet('{HARVEST_DIR}/strings.parquet')
          WHERE NOT regexp_matches(kind, '{EXCLUDE_KINDS}')
        UNION ALL SELECT value, count AS nb, kind FROM read_parquet('{HARVEST_DIR}/paths.parquet')
          WHERE kind <> 'path.owner'
        UNION ALL SELECT value, count(DISTINCT sha256) AS nb, kind FROM read_parquet('{HARVEST_DIR}/ocr_occurrences.parquet')
          GROUP BY value, kind""")
    con.execute(f"""CREATE TABLE inp AS
        SELECT tpl, any_value(t) AS text, sum(nb) AS nb, list(DISTINCT kind)[1:8] AS kinds FROM (
          SELECT {norm} AS t, regexp_replace({norm}, '[0-9]+', '0', 'g') AS tpl, nb, kind FROM src
          WHERE length(value) BETWEEN 3 AND 300 AND regexp_matches(value, '{NAMEISH}'))
        GROUP BY tpl""")
    con.execute(f"""COPY (SELECT row_number() OVER (ORDER BY length(text), text) AS tid, text, nb, kinds FROM inp)
                    TO '{NER_DIR}/input.parquet' (COMPRESSION zstd)""")
    print(con.execute(f"SELECT count(*), sum(nb) FROM '{NER_DIR}/input.parquet'").fetchall())


# ---- prefilter: only strings that can contain a name -------------------------------------
CJK_RUN = re.compile(r"[぀-ヿ㐀-鿿가-힯]{2,}")
CODE_KIND = re.compile(r"^text\.(?!txt$|md$|html\.text$|rtf$|tex$|unknown$)")  # source code, KiCad, configs...
CODE_KEYWORDS = re.compile(r"(?i)copyright|\(c\)|©|author|written by|maintainer|licen[cs]ed? to|\binc\b|\bltd\b|gmbh|"
                           r"\bllc\b|universit|institut|manufacturer|vendor|supplier|company|corp|@|https?://|designed by|"
                           r"drawn by|checked by|approved by|engineer|designer")
NOISE_KINDS = re.compile(r"^(ole\.entry|f3d\.member|f3z\.member|sat\.header|sw\.section|fcstd\.member)$")
_IDTOK = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]+$")
PIECE = 120


def _candidate_token(w: str, dic, keep) -> bool:
    if len(w) < 3 or _IDTOK.match(w):
        return False
    lw = w.lower()
    if lw in dic.names:
        return True
    if lw in dic:
        return False
    from .entities import key
    return (w[0].isupper() or w.isupper()) and key(w) not in keep


def wanted(text: str, kinds: list, dic, keep) -> bool:
    k0 = (kinds or ["?"])[0]
    if NOISE_KINDS.match(k0):
        return bool(CJK_RUN.search(text)) or "@" in text
    if CODE_KIND.match(k0):
        return bool(CODE_KEYWORDS.search(text))
    return bool(CJK_RUN.search(text)) or "@" in text or \
        any(_candidate_token(w, dic, keep) for w in re.findall(r"[^\W\d_]{3,}", text))


def pieces(text: str) -> list[str]:
    """Split long strings at whitespace into <= PIECE chars (attention cost is super-linear)."""
    out = []
    while len(text) > PIECE:
        cut = text.rfind(" ", PIECE // 2, PIECE)
        cut = cut if cut > 0 else PIECE
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out + ([text] if text else [])


def build_pieces() -> None:
    from .entities import dictionary, keep_keys
    dic, keep = dictionary(), keep_keys()
    t = pq.read_table(NER_DIR / "input.parquet").to_pylist()
    rows = [(r["tid"], p) for r in t if wanted(r["text"], r["kinds"], dic, keep) for p in pieces(r["text"])]
    rows.sort(key=lambda x: len(x[1]))
    pq.write_table(pa.table({"pid": pa.array(range(len(rows)), pa.int64()), "tid": pa.array([r[0] for r in rows], pa.int64()),
                             "text": pa.array([r[1] for r in rows])}), NER_DIR / "pieces.parquet")
    print(dict(inputs=len(t), pieces=len(rows), mchars=round(sum(len(r[1]) for r in rows) / 1e6, 1)))


_model = None


def _init():
    global _model
    import warnings
    warnings.filterwarnings("ignore")
    import torch
    torch.set_num_threads(int(os.environ.get("NER_THREADS", "1")))
    from gliner import GLiNER
    _model = GLiNER.from_pretrained(MODEL)
    _model.eval()


def _work(chunk):
    pids, tids, texts = chunk
    res = _model.inference(list(texts), LABELS, threshold=THRESHOLD, batch_size=32)
    return pids, [(tid, e["text"], e["label"], float(e["score"]), int(e["start"]), int(e["end"]))
                  for tid, ents in zip(tids, res) for e in ents]


def run(workers: int, chunk: int = 64) -> None:
    """Resumable. Earlier whole-string runs (done-*.parquet holds tids) count as done for their tids."""
    t = pq.read_table(NER_DIR / "pieces.parquet").to_pylist()
    done_t, done_p = set(), set()
    for p in NER_DIR.glob("done-*.parquet"):
        d = pq.read_table(p)
        (done_p if "pid" in d.column_names else done_t).update(d.column(0).to_pylist())
    todo = [r for r in t if r["pid"] not in done_p and r["tid"] not in done_t]
    chunks = [tuple(zip(*[(r["pid"], r["tid"], r["text"]) for r in todo[i:i + chunk]])) for i in range(0, len(todo), chunk)]
    print(f"ner todo pieces={len(todo)} chunks={len(chunks)} mchars={sum(len(r['text']) for r in todo)/1e6:.1f}", flush=True)
    rows, dn, t0, n = [], [], time.time(), 0

    def flush():
        if not dn:
            return
        tag = f"{len(list(NER_DIR.glob('done-*.parquet'))):05d}"
        cols = list(zip(*rows)) if rows else [[]] * 6
        pq.write_table(pa.table([pa.array(c, f.type) for c, f in zip(cols, SCHEMA)], schema=SCHEMA), NER_DIR / f"spans-{tag}.parquet")
        pq.write_table(pa.table({"pid": pa.array(dn, pa.int64())}), NER_DIR / f"done-{tag}.parquet")  # written last = commit
        rows.clear()
        dn.clear()

    with get_context("fork").Pool(workers, initializer=_init) as pool:
        for pids, r in pool.imap_unordered(_work, chunks):
            rows.extend(r)
            dn.extend(pids)
            n += len(pids)
            if len(dn) >= 10000:
                flush()
                el = time.time() - t0
                print(f"{n}/{len(todo)} {el/60:.1f} min  {n/el:.0f}/s", flush=True)
    flush()
    print(f"ner finished in {(time.time()-t0)/60:.1f} min", flush=True)


def spans() -> pa.Table:
    """All spans joined to their input text / blob count / kinds."""
    import duckdb
    return duckdb.sql(f"""SELECT s.*, i.text AS source, i.nb, i.kinds FROM read_parquet('{NER_DIR}/spans-*.parquet') s
                          JOIN read_parquet('{NER_DIR}/input.parquet') i USING (tid)""").to_arrow_table()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["input", "pieces", "run"])
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    {"input": build_input, "pieces": build_pieces}.get(a.cmd, lambda: run(a.workers))()
    sys.exit(0)
