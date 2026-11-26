# Stage 1: string harvest

**Goal:** every string in the corpus that *could* name a person, company or brand, recorded with
where it came from (format and field) and how often it occurs. Nothing is decided here; Stage 2
classifies the strings and builds the pseudonym map.

Code:
- `deid/harvest.py`: the per-format harvesters.
- `deid/harvest_run.py`: the parallel runner, path harvest and aggregation.
- `deid/ocr_run.py`: the raster OCR pass.
- `tests/test_harvest.py`: the harvester tests.

## Inputs
- **Jobs:** 167,253 unique sha256 (100.2 GB):
  - 147,813 staged blobs, minus archives, which are processed through their members;
  - 22,519 unpacked archive members not already present as blobs.
- **Dedup:** each sha256 is harvested once, and the results are joined back to keys through `blobs.parquet` and `members.parquet`.

## Harvesters: what is read, per format
Geometry is never read: no coordinates and no mesh data. Only string-typed fields, names, comments, metadata and text runs are kept.

| Format | Harvested | `kind` prefix |
|---|---|---|
| STEP | `FILE_NAME` name / **author** / **org** / system, `FILE_DESCRIPTION`; every string literal in DATA (`PRODUCT`, shape names). Decodes `\X2\` / `\X4\` / `\X\` / `\S\` escapes, then UTF-8 → GBK → cp1252 | `step.*` |
| IGES | Start section; Global section parsed positionally (3 product, 4 file, 5 system, **21 author**, **22 org**); Hollerith strings in the P section | `iges.*` |
| ACIS SAT | Header lines; `@n` strings (v7+); word tokens for older versions. SAB → text runs | `sat.*` |
| Parasolid x_t | Header `KEY=value` (**USER**, **SITE**, …), tokens | `xt.*` |
| STL | ASCII `solid` / `endsolid` names; 80-byte binary header | `stl.*` |
| OBJ / OFF / VRML | Comments, `o` / `g` / `usemtl` / `mtllib`; VRML `DEF` / `USE` names and strings | `obj.*`, `vrml.*` |
| DXF | Code/value pairs for text codes (1–8, 300–309, 999, 1000–1003); header variables as `dxf.hdr.$VAR`; MTEXT formatting removed | `dxf.*` |
| DWG | ODA File Converter → DXF at the same version → the DXF harvester | `dwg.*` |
| PostScript / EPS | DSC comments (`%%For`, `%%Creator`, …), comments, `( … )` strings | `ps.*` |
| Creo `#UGC` | Header line, HOST / DBID / model name, ASCII text runs | `creo.*` |
| SolidWorks 2015+ | Every section inflated and CRC-checked; XML property values (`sw.xml.prop.Author`, `SW-Author`, `CompanyName`, …); ASCII, UTF-16 and CJK text runs; section names | `sw.*` |
| OLE2 (SW < 2015, Inventor, Solid Edge, Altium, …) | SummaryInformation (**author**, **last_saved_by**, **company**, …); storage and stream names; `__ZLB` streams decompressed; text runs | `ole.*` |
| FCStd / 3MF / f3d / f3z / ODG | Zip members: XML text, attribute values and named properties (`fcstd.xml.prop.CreatedBy`, 3MF `<metadata name=Designer>`); member names; binary members as runs. Nested f3d inside f3z is handled; 3MF vertex/triangle lists are skipped | `<fmt>.*` |
| PDF | Info dictionary, XMP, page text lines, TOC, annotations, form fields, links, embedded file names | `pdf.*` |
| SVG / XML / HTML | Text nodes, attribute values (geometry attributes skipped), comments, `dc:creator` etc. | `svg.*`, `text.<ext>.*` |
| Images | JPEG APPn/COM, PNG tEXt/iTXt/zTXt/eXIf, XMP. Pixels go to the OCR pass | `image.*` |
| JT | Text runs plus runs inside zlib-compressed segments | `jt.*` |
| JSON / GeoJSON | String values and keys, with escapes decoded | `json.*` |
| Other text (source code, KiCad, Gerber, UNV, …) | Lines containing letters | `text.<ext>` |
| Other binary (CATIA, Parasolid binary, exe / dll, …) | ASCII, UTF-16 and UTF-8 CJK runs | `bin.<ext>` |
| Paths | Every key component: owner, repo, directories, file names; archive member paths | `path.*`, `member.*` |

**Text runs from binary data:**
- ASCII and UTF-16LE runs of at least 4 characters, kept if they pass a plausibility filter (at least 3 letters, mostly alphanumeric).
- UTF-16 and UTF-8 CJK runs are kept only when at least 80% of the ideographs are common ones (GB2312 level 1) or kana. ASCII bytes read as UTF-16 otherwise look like Chinese.
- The UTF-16 CJK scan decodes both byte alignments. An earlier version lost alignment and cut words short (`Centur`); this was found in sampling and fixed.

## Outputs
In `~/data/machina/harvest/`, also uploaded to `s3://machina-deid-raw-arav-065371277807/_harvest/`:

| File | Rows | Content |
|---|---|---|
| `jobs.parquet` | 167,253 | sha256, format, size, S3 key, origin (blob / member) |
| `status.parquet` | 167,253 | per-file status, seconds, number of unique strings, images, scanned pages, notes |
| `occurrences.parquet` | 117.4 M | (sha256, kind, value, count): where each string occurs |
| `strings.parquet` | 50.2 M | (kind, value): total count and number of blobs |
| `values.parquet` | 45.1 M | value: number of blobs, count, list of kinds |
| `paths.parquet` | 206 k | path components by kind, with key counts |
| `ocr_occurrences.parquet` | 152 k | (sha256, kind, value, count, score) from OCR, score ≥ 0.6 |
| `ocr_status.parquet` | 5,088 | per-file OCR status, images OCR'd, pages rendered |
| `harvest_summary.json` | | per-format totals, failures, truncated files |

## Results
- **Files:** 167,253 of 167,253 harvested.
  - First pass: 1 timeout, a 235 MB SolidWorks assembly; and 1 error, a password-protected PDF. Both succeeded on `--retry-failed`. The PDF now falls back to raw text runs.
- **Time:** 41 minutes wall-clock with 15 workers on the 16-vCPU box; 10.1 CPU-hours. The 100 GB were downloaded from S3 inside the region.
- **Truncation:** 32 files reached the per-file cap of 150 k unique strings. They are 17 large SolidWorks assemblies, plus `.arc`, `.gto` and `.exe` files, and are listed in `harvest_summary.json`. Stage 3 rewrites those files with the full map and Stage 4 re-scans every output, so the cap can't cause a leak.
- **Volume:** 45 M unique values.
  - 33.9 M appear in only 1 blob and 9.2 M in 2–4 blobs.
  - 35.1 M come only from binary text runs, mostly SolidWorks (42 M per-file uniques) and Creo (23 M).
  - Stage 2 therefore classifies **field strings first**, and treats binary runs only through regex, gazetteer and dictionary matching. It does not send them to NER or an LLM in bulk.

### Recall probes (entities known from Stage 0 and the spikes)
| Entity | Distinct values containing it | Blob hits |
|---|---|---|
| `hackmancoltaire` | 54 | 2,419 |
| `ALBERTO PARMIGGIANI` | 26 | 4,561 |
| `pcorreia` | 5 | 11,278 |
| `TraceParts` | 141 | 1,440 |
| `Guilherme_Grancho` | 93 | 287 |
| `CRANE-X7` | 110 | 221 |
| `albertpan98@qq.com` | 1 | 73 |
| `MSI\alexa (MSI)` | 1 | 97 |
| `青龙` | 9 | 10 |
| `usnistgov`, `loongOpen`, `QingLoong`, `Himalia13` | path components only (`paths.parquet`) | |

### High-signal fields (examples; the most frequent value per field)
- `step.file_name.author` / `org`:
  - generic: `Author`, `Open CASCADE`, `step.parts`;
  - names: `Rene Poeschl`, `poeschlr@gmail.com`, `ZWCAD Software Co.`.
- `iges.global.author`: `dmgaines`, `Jeff Hudgens`, `Steve`.
- `ole.meta.author` / `last_saved_by`: `LECOMTE`, `Garth Zeglin, 412-268-5909, garthz@cmu.edu`, `McMaster-Carr Supply Company`, `黄海生`, `Guille`, `epallares`, `Tony`.
- `pdf.meta.author`: `3DAssist-PC\3DAssist`, `EPALLARES\epallares`, `MSI\alexa`, `WIN\mbokj`.
- SolidWorks properties: `hackmancoltaire` (2,299 files), `SixWingedFreak`, `Magnus Bolt Kjer` / `DTU`, `CADENAS GmbH`.
- FreeCAD `CreatedBy`: `Simone Bignetti`, `Anthony W. Hursh`, `Jesus Ignacio Fernandez`.
- Parasolid header: `USER=brown` / `kramer` / `regli`, `SITE=sdl-cambridge-u.k.`.

### OCR pass (`deid/ocr_run.py`, RapidOCR PP-OCRv6 on ONNX, CPU, Chinese + Latin)
- **Targets:** 5,088 files:
  - 3,426 PDFs that contain images or scanned pages;
  - 1,662 standalone images (png, jpeg, gif, tiff).
- **Status:** 4,977 ok. 111 `not_image` are FrontPage `_vti_cnf` metadata text files named `*.gif` / `*.jpg`; their text, including `vti_author`, is already in the main harvest.
- **Work done:** 24,382 images OCR'd and 1,834 scanned pages rendered. Pages are rendered at 150 dpi, reduced for very large formats so they stay under 40 MP. Huge scans are downscaled, not skipped.
- **Time:** 15.2 CPU-hours, about 80 minutes wall-clock.
  - The first run oversubscribed the CPUs (load 43 on 16 vCPU), because OpenCV's thread pool ignores the ONNX thread setting. It was fixed with `cv2.setNumThreads(1)`.
- **Output:** 152 k occurrences, of which **81 k values are not found anywhere in the text harvest**. Examples, mostly logos and raster title blocks:
  - `Universidad Carlos III de Madrid` (127 files)
  - `ETH zürich` (117)
  - `Howard Hughes Medical Institute` / `janelia` (71)
  - `CC-BY 4.0, Philippe COLBACH, 2024` (69)
  - `BARCELONATECH`
  - `HTL ST. PÖLTEN`
  - `PRECICU`

  Text-layer harvesting alone would have missed them.

## Findings for later stages
1. **DWG `$LASTSAVEDBY` is not recoverable through ODA.** The converter stamps its own OS user into the output, so all 558 DWGs report `ubuntu`.
   - Stage 3's DWG writer round-trips through ODA, so this field and the DWG summary info, which DXF doesn't carry, are replaced by construction.
   - The container must run as a neutral user (`deid`).
   - The original values stay unknown, which is acceptable since they are overwritten rather than published. If needed, LibreDWG `dwgread` could read them (building it was not approved earlier).
2. **Thumbnails:** 238,685 embedded images were counted (36 k SolidWorks previews, 188 k PDF images, and others).
   - CAD thumbnails are not OCR'd. Stage 3 neutralises them in length-sensitive formats and scrubs them in re-encodable ones.
   - OCR covers standalone images, scanned PDF pages (2,523) and PDF-embedded images.
3. **Generic accounts:** `User`, `Administrator`, `Admin`, `Operator`, `ubuntu`, `Author`, `Unknown`, `NONE` dominate the field values. They are covered by `GENERIC_ACCOUNTS` in `deid/replace.py`, to be extended in Stage 2 with `Operator`, `Author`, `Title` and `NONE`.
4. **Tool and vendor strings** (`Open CASCADE`, `SOLIDWORKS 2018 SP4.0`, `PScript5.dll`, `CADENAS GmbH`, `TraceParts`, `McMaster-Carr`): Stage 2 must decide whether they are identifying. The plan's assumption is that CAD-tool brands are kept and content-provider / vendor names are replaced. This needs your confirmation before Stage 2.
