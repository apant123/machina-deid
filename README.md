# machina-deid

De-identification pipeline for engineering/CAD data
(R2 `machina-deid-challenge-v1/sources/` → AWS, us-east-2).

## Setup
- Python **3.12**, managed with uv. The project venv is `.venv312`; the older `.venv` (3.14) is unused.
  - `UV_PROJECT_ENVIRONMENT=.venv312 uv sync -p 3.12`
  - uv lives at `~/.uv-tool/bin/uv`.
- Secrets: the R2 credentials are read from the environment or from `.env`, an owner-only file that is git-ignored and never uploaded. The variables are:
  - `R2_ENDPOINT`
  - `R2_ACCESS_KEY_ID`
  - `R2_SECRET_ACCESS_KEY`
  - `R2_BUCKET`
  - `R2_PREFIX`
- AWS: SSO profile `machina` (account 065371277807, us-east-2).
- Local working data: `~/data/machina/` (override with `DEID_DATA_DIR`).

## Buckets
| Bucket | Purpose |
|---|---|
| `machina-deid-raw-arav-065371277807` | Private staging copy of the R2 source, same keys. `_manifest/` holds the manifest |
| `machina-deid-output-arav-065371277807` | De-identified output (mirrors `sources/`) |
| `machina-deid-code-arav-065371277807` | Source code + documentation |

## Stage 0: ingest, manifest, dedup
```bash
.venv312/bin/python -m deid.stage --workers 96   # R2 -> S3 staging, sha256 on the fly; resumable
.venv312/bin/python -m deid.manifest             # manifest.parquet + blobs.parquet + summary -> s3://…raw…/_manifest/
.venv312/bin/python -m deid.unpack --workers 16  # safely unpack archives lacking .extracted/ -> _unpacked/<sha256>/
.venv312/bin/python -m deid.unpack --retry-failed # re-run only failed (non-encrypted) archives, merge results
.venv312/bin/python -m pytest -q tests/           # unpack safety-guard tests
.venv312/bin/python -m tools.inspect_r2          # (optional) re-run the corpus audit -> r2_audit.json
```
System packages needed: `p7zip`/`7zip` + `7zip-rar` (RAR codec), `ncompress`.

Format spike results (SolidWorks 2015+, OLE2, Creo, DWG): see [docs/SPIKES.md](docs/SPIKES.md).

`deid.stage` streams each object once:
- It hashes the object and captures its first 512 bytes while uploading.
- Results are appended to `~/data/machina/manifest/stage_results.jsonl`.
- Re-running skips keys already staged.
- `.resume-partial` files (truncated downloads) are not staged.

The manifest outputs:
- **`manifest.parquet`**, one row per key. Columns:
  - size, R2 ETag, sha256
  - the format detected from magic bytes (`deid/formats.py`)
  - the GitHub/GitLab owner, repo and commit taken from the path
  - archive ↔ `.extracted/` pairing (`extracted_sibling`, `from_archive`)
  - dedup fields (`canonical_key`, `dup_count`)
  - `action`: process / copy / skip / retry
- **`blobs.parquet`**, one row per unique sha256, with all the keys that share it. Later stages process each blob once and copy the result to every key.

## Stage 1: string harvest
```bash
.venv312/bin/python -m deid.harvest_run jobs            # harvest/jobs.parquet: unique blobs + unpacked members
.venv312/bin/python -m deid.harvest_run paths           # key / member path components (no download)
.venv312/bin/python -m deid.harvest_run run --workers 15  # resumable; stops cleanly when SSO expires
.venv312/bin/python -m deid.harvest_run run --retry-failed --workers 2   # 6x timeout for failures
.venv312/bin/python -m deid.harvest_run aggregate       # DuckDB, out-of-core
.venv312/bin/python -m deid.ocr_run run --workers 7     # OCR: images, scanned PDF pages, PDF-embedded images
.venv312/bin/python -m deid.ocr_run aggregate
.venv312/bin/python -m deid.harvest_run upload          # -> s3://…raw…/_harvest/
```
`Xvfb` + ODA File Converter are needed for DWG (the runner starts a shared `Xvfb :99`). See [docs/STAGE1.md](docs/STAGE1.md) for the formats, the outputs and the results.

## Stage 2: entity resolution → pseudonym map
```bash
.venv312/bin/python -m deid.ner_run input               # unique human-text strings, digit-templated
.venv312/bin/python -m deid.ner_run pieces              # prefilter (proper-noun token / CJK / @) + split ≤120 chars
NER_THREADS=2 .venv312/bin/python -m deid.ner_run run --workers 7   # GLiNER multi, local CPU, resumable
.venv312/bin/python -m deid.entities build              # owners + fields + regex + vendors + NER -> map_candidates
.venv312/bin/python -m deid.entity_scan run --workers 6 # corpus hit counts (Aho-Corasick) -> map.parquet
.venv312/bin/python -m deid.spotcheck                   # -> ~/data/machina/entities/spotcheck.md (contains PII)
.venv312/bin/python -m pytest -q tests/
```
Policy lists (tools kept, vendors replaced, generic accounts, infrastructure domains) live in
`config/entity_policy.yaml`. The seed (`DEID_SEED`) is in `.env`, and the map itself is PII. Neither goes to
the code bucket. See [docs/STAGE2.md](docs/STAGE2.md).

Before uploading code or docs anywhere: `python -m deid.code_leak_check` must print `no leaks`. It scans the repo for every distinctive original name in the map, and for their pseudonyms. The vendor gazetteer, the reviewer seeds and the probes live in `<DATA_DIR>/entities/` (private). `r2_audit.json` is never shipped.
