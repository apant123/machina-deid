"""Stage 2d: spot-check report of the pseudonym map (contains real names -> private data dir only).

    python -m deid.spotcheck   # -> ~/data/machina/entities/spotcheck.md
"""
from __future__ import annotations

import random
import sys
from collections import Counter

import pyarrow.parquet as pq
import yaml

from .entities import ENT_DIR
from .pseudonym import CJK_RANGE



def _esc(s, n=60) -> str:
    s = str(s if s is not None else "")
    s = s.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    s = "".join(c if c.isprintable() else "·" for c in s)
    return s[:n] + ("…" if len(s) > n else "")


def _table(rows, cols, n=60) -> str:
    out = ["| " + " | ".join(c for c, _ in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(_esc(f(r), n) for _, f in cols) + " |")
    return "\n".join(out)


MAPCOLS = [("hits (values / blobs)", lambda r: f"{r['hit_values']:,} / {r['hit_blobs']:,}"),
           ("original", lambda r: r["surface"]), ("pseudonym", lambda r: r["pseudonym"]),
           ("type", lambda r: r["etype"]), ("scope", lambda r: r["scope"]), ("sources", lambda r: r["sources"]),
           ("where", lambda r: r["hit_where"].replace("'", "")), ("example context", lambda r: r["examples"].split(" ¦ ")[0])]


def main() -> None:
    m = pq.read_table(ENT_DIR / "map.parquet").to_pylist()
    dec = pq.read_table(ENT_DIR / "decisions.parquet").to_pylist()
    ents = pq.read_table(ENT_DIR / "entities.parquet").to_pylist()
    random.seed(7)
    L = ["# Stage 2 spot-check: pseudonym map", "",
         f"- Entities: **{len(ents):,}**; map rows (surface → pseudonym): **{len(m):,}**",
         f"- By type: {dict(Counter(r['type'] for r in ents).most_common())}",
         f"- By source (entity may have several): {dict(Counter(s for r in ents for s in r['sources']).most_common())}",
         f"- Map rows by scope: {dict(Counter(r['scope'] for r in m))} (global = word-bounded anywhere; field = whole "
         f"field value / profile-folder or owner component only)",
         f"- Width check (same length in UTF-8 / UTF-16 / cp1252 / GBK): {sum(r['widths_ok'] for r in m):,}/{len(m):,} ok",
         f"- Decisions not in map: {dict(Counter((d['decision'], d['reason']) for d in dec).most_common(12))}", ""]

    top = sorted(m, key=lambda r: -r["hit_blobs"])[:100]
    L += ["## 1. Top 100 replacements by number of blobs hit", "", _table(top, MAPCOLS), ""]

    short = [r for r in m if len(r["surface"]) <= 5 and not CJK_RANGE.search(r["surface"]) and r["hit_values"]]
    L += [f"## 2. Short names (≤ 5 chars): random 50 of {len(short):,}", "",
          _table(sorted(random.sample(short, min(50, len(short))), key=lambda r: -r["hit_blobs"]), MAPCOLS), ""]

    cjk = [r for r in m if CJK_RANGE.search(r["surface"])]
    L += [f"## 3. CJK strings: {len(cjk):,} rows (top 60 by blobs)", "",
          _table(sorted(cjk, key=lambda r: -r["hit_blobs"])[:60], MAPCOLS), ""]

    xl = [r for r in ents if r.get("xlink")]
    L += [f"### 3b. Cross-script links (pinyin): {len(xl)}", "",
          _table(xl[:40], [("entity", lambda r: r["entity"]), ("linked to", lambda r: r["xlink"]), ("type", lambda r: r["type"]),
                           ("surfaces", lambda r: ", ".join(r["surfaces"][:4]))]), ""]

    ocr_only = [r for r in m if r["hit_where"] and "ocr" in r["hit_where"] and "content" not in r["hit_where"]
                and "path" not in r["hit_where"]]
    ocr_any = [r for r in m if "ocr" in (r["hit_where"] or "")]
    L += [f"## 4. OCR hits: {len(ocr_only):,} map rows found only in OCR text ({len(ocr_any):,} incl. OCR anywhere)", "",
          _table(sorted(ocr_only, key=lambda r: -r["hit_blobs"])[:60], MAPCOLS), ""]

    # known entities to probe for recall: private list (real names) -> ENT_DIR/probes.yaml, not in code
    PROBES = yaml.safe_load((ENT_DIR / "probes.yaml").read_text()) if (ENT_DIR / "probes.yaml").exists() else []
    L += ["## 5. Recall probes (known entities)", ""]
    idx = {r["surface"].casefold(): r for r in m}
    prow = []
    for p in PROBES:
        r = idx.get(p.casefold())
        if r:
            prow.append(dict(p=p, f=r["pseudonym"], t=r["etype"], s=r["scope"], h=f"{r['hit_blobs']:,}", src=r["sources"]))
        else:
            near = [x for x in m if p.casefold() in x["surface"].casefold()][:3]
            prow.append(dict(p=p, f="— not a key; contained in: " + "; ".join(f"{x['surface']}→{x['pseudonym']}" for x in near)
                             if near else "**MISSING**", t="", s="", h="", src=""))
    L += [_table(prow, [("probe", lambda r: r["p"]), ("pseudonym", lambda r: r["f"]), ("type", lambda r: r["t"]),
                        ("scope", lambda r: r["s"]), ("blobs", lambda r: r["h"]), ("sources", lambda r: r["src"])], 90), ""]

    drops = sorted([d for d in dec if d["decision"] == "drop"], key=lambda d: -(d["nb"] or 0))
    L += ["## 6. Recall check: highest-frequency candidates that were DROPPED (top 60)", "",
          _table(drops[:60], [("blobs", lambda d: d["nb"]), ("candidate", lambda d: d["surface"]), ("label", lambda d: d["etype"]),
                              ("score", lambda d: "" if d.get("score") is None else f"{d['score']:.2f}"),
                              ("source", lambda d: d["source"]), ("reason", lambda d: d["reason"])]), "",
          "Random 40 dropped NER spans:", "",
          _table(random.sample([d for d in drops if d["source"] == "ner"], min(40, sum(d["source"] == "ner" for d in drops))),
                 [("blobs", lambda d: d["nb"]), ("candidate", lambda d: d["surface"]), ("label", lambda d: d["etype"]),
                  ("score", lambda d: "" if d.get("score") is None else f"{d['score']:.2f}"), ("reason", lambda d: d["reason"])]), ""]
    kept = sorted([d for d in dec if d["decision"] == "keep"], key=lambda d: -(d["nb"] or 0))
    L += ["## 7. Kept by policy (tools / generic / standards): top 40", "",
          _table(kept[:40], [("blobs", lambda d: d["nb"]), ("value", lambda d: d["surface"]), ("as", lambda d: d["etype"]),
                             ("reason", lambda d: d["reason"])]), ""]
    L += ["## 8. Random 60 map rows (precision sample)", "",
          _table(random.sample([r for r in m if r["hit_values"]], 60), MAPCOLS), ""]
    (ENT_DIR / "spotcheck.md").write_text("\n".join(L))
    print(ENT_DIR / "spotcheck.md")


if __name__ == "__main__":
    main()
    sys.exit(0)
