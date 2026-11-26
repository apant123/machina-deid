"""Guard for the code/docs bucket: no original surface from the pseudonym map may appear in the repo.

    python -m deid.code_leak_check   # exit 1 + list of hits (file:line: surface) if anything leaks
Checked: distinctive originals (global scope, >= 5 chars, not a dictionary word) AND their pseudonyms
(a pseudonym in the docs reveals one real -> fake pair). r2_audit.json is not shipped and is skipped.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pyarrow.parquet as pq

from .config import ROOT
from .entities import ENT_DIR, dictionary

NOT_SHIPPED = {"r2_audit.json"}  # raw-bucket audit (real owner names): kept out of the code bucket
SKIP = {".venv", ".venv312", "__pycache__", ".git", ".pytest_cache", ".ruff_cache"}
EXT = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json", ".sh", ".cfg", ".ini", ""}


def main() -> int:
    import ahocorasick
    dic = dictionary()
    A = ahocorasick.Automaton()
    for r in pq.read_table(ENT_DIR / "map.parquet").to_pylist():
        s = r["surface"]
        if r["scope"] == "global" and len(s) >= 5 and s.lower() not in dic and r["hit_values"]:
            A.add_word(s.lower(), s)
            f = r["pseudonym"]
            if len(f) >= 5 and f.lower() not in dic and f.lower() != s.lower():
                A.add_word(f.lower(), f"{f}  (pseudonym)")
    A.make_automaton()
    hits = []
    for f in sorted(ROOT.rglob("*")):
        if not f.is_file() or SKIP & set(f.relative_to(ROOT).parts) or f.suffix not in EXT or f.stat().st_size > 5e6 \
                or f.name in NOT_SHIPPED:
            continue
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            low = line.lower()
            for end, s in A.iter(low):
                n_ = len(s.split("  (")[0])
                a, b = end - n_, end + 1
                if (a < 0 or not low[a].isalnum()) and (b >= len(low) or not low[b].isalnum()):
                    hits.append(f"{f.relative_to(ROOT)}:{n}: {s}")
    print("\n".join(hits) or "no leaks")
    print(f"{len(hits)} hits in {len({h.split(':')[0] for h in hits})} files", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
