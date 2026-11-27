# Stage 3: rewrite (per-format writers)

> Names, handles and e-mails in the examples below are **synthetic stand-ins**. The real values stay only in the private data directory and staging bucket (`deid.code_leak_check` enforces this).

**Goal:** every unique blob is de-identified once, by a writer for its real format (detected from magic bytes), and written to every key that holds it, under the renamed key. Geometry is never touched. A file that cannot be rewritten safely is **quarantined**: it is not shipped, and it is listed in the report.

Code:
- `deid/rewrite/mapping.py`: the map as byte-level replacers, plus key renaming.
- `deid/rewrite/dispatch.py`: format routing.
- `deid/rewrite/{text,sw,ole,binary,containers,pdf,image,thumbs,dwg}.py`: the writers.
- `deid/logo_scan.py`: the logo pass.
- `deid/rewrite_run.py`: the runner.
- `tests/test_rewrite.py`: the tests.
- `infra/`: the Docker image and AWS Batch job definition.

## The map at byte level (`DeidMap`)
**Three passes**, all length-preserving, because every pseudonym has the original's width in UTF-8, UTF-16, cp1252 and GBK:

| Scope | Replacer mode | Where it applies |
|---|---|---|
| global | word-bounded | anywhere |
| field | whole value | a complete delimited value (quoted, XML text, length-prefixed string) or a path component |
| path | component | only between `\` or `/` separators (user names of ≤ 3 characters) |

**The `Replacer` uses Aho-Corasick** over a latin-1 view of the bytes. The earlier regex alternation could not handle 14k keys: it took more than 10 minutes on 10 MB. The new one builds in 0.1 s and runs at about 8 MB/s on worst-case input.

**Word boundaries:**
- **Hard boundaries** are non-alphanumerics, and a 1-byte length prefix counts as one.
- **Soft boundaries** (letter↔digit, lowercase→Uppercase, acronym→CamelCase word) are allowed for keys of 5 or more characters, on **one side only**. This replaces `PartsvaultStandard` and `Quorvex Bt3`, but never cuts into an identifier such as `moQuorvexUnits`.
- **Plural and possessive** forms (`Quorvexes`) are replaced.
- **Distinctive keys** (8 or more characters with a non-letter) need only one hard edge.

**Short keys** (4 characters or fewer) are replaced only when they form:
- a whole string: a CString, VT_LPSTR / VT_LPWSTR, an MFC Unicode string, an NX record, or delimited text;
- the first word of a length-prefixed string (`ZORB_TK_04…`);
- a name followed by 1–3 digits and a separator (`ZORB3_006_G`).

**Glued suffixes** (added after the sample residual review), all requiring a hard start:
- a key followed by `_LOGO` / `_logo` (`ZORB_LOGO`, `zorb_logo.sym`);
- an upper-case product key containing a digit (5 or more characters) followed by **one** upper-case model letter (`XQZ3DR v2`);
- the short-key "name + 1–3 digits" rule also accepts a NUL / control-byte terminator after the digits. Its left edge accepts anything but a letter, because Creo records often put a printable byte before a part name (`<ZORB3_006_A_001_HEAD.ASM`).

The XML-name guard now treats `<name` as a tag only when the name ends the way a tag name does (whitespace, `>` or `/`).

**Reference stems** run as the first pass. Every renamed object whose file stem is distinctive (ASCII, 8 or more characters, with a digit, `_` or `-`) maps exactly to its renamed stem, 2,962 stems in all. An in-file reference to a renamed part is therefore always rewritten to the renamed part's name, even where the word rules cannot see the name. `plan` builds the key renames without this pass, so renames never depend on a previous plan.

**Host names** (`Hostname: '…'`, Creo `#- HOST`) are opaque values. Inside them, keys of 4 or more characters are replaced as substrings (`QUORVEXDESKTOP`).

**Structure guards (unchanged from the eDrawings fix):** MFC class names and XML tag and attribute names are never modified.

**Key renaming** uses the same rules, component by component:
- `owner__repo` parts count as whole values;
- in-file references and renamed objects therefore stay byte-identical, so assemblies keep resolving their parts.

`plan` checks that no two different keys collide after renaming.

**Residual check** (reported, not enforced here; Stage 4 runs the full leak scan):
- an original key that *starts a word* in the text runs of the output (`teodo` in `9900417_Teodorico`);
- words that merely contain a key mid-word (`isRedemption`) are not counted.

## Writers
| Format(s) | Writer | Same size | Notes |
|---|---|---|---|
| STEP, IGES, SAT, x_t, DXF, SVG, OBJ, VRML, UNV, ASCII STL, PS, Creo neutral, BREP, SCAD, OFF, other text | `text.py` | yes | Streamed in 32 MB chunks cut at newlines. **STEP:** `\X2\` / `\X4\` / `\X\` / `\S\` literals are decoded, replaced and re-encoded with the same escape kind per character. **IGES:** data columns are joined per section and Hollerith strings treated as whole values, so the 80-column layout and counts stay valid. **DXF:** `\U+hhhh` escapes |
| Binary STL | `text.py` | yes | Only the 80-byte header |
| SolidWorks 2015+ | `sw.py` | yes | Each section is inflated, replaced, and deflated to **exactly** the original length (zlib grid → split flushes → stored prefix → zopfli). CRCs are patched in every header that records the section. Section names are renamed in place. Previews become neutral images; if one cannot be fitted, the original pixels are kept and counted. If any other section cannot be fitted, the file is quarantined |
| OLE2 (SW < 2015, Inventor, Solid Edge, NX, …) | `ole.py` | yes | Each stream is rewritten along its own FAT / mini-FAT chain. `__ZLB` streams are recompressed to their exact size. Preview streams and SummaryInformation DIB thumbnails are neutralised. Free sectors, free mini-sectors, tail slack and empty directory entries are **zeroed**. The file is re-opened to verify the stream list, sizes and readability |
| Creo `#UGC` | `binary.py` | yes | The map, plus embedded JPEG thumbnails replaced with same-size neutral JPEGs. The header offset to `#END_OF_P_OBJECT` is re-checked |
| JT | `binary.py` | yes | Also replaces inside zlib segments, recompressed to their exact size |
| Parasolid binary, U3D, CATIA, I-DEAS, 3DS, native prt/asm/drw, unknown binary | `binary.py` | yes | The map plus embedded JPEG thumbnails |
| FCStd, 3MF, f3d, f3z, ODG, zip | `containers.py` | no | Each member goes through its own writer (recursive). Member names are renamed with the key rules. Order, compression method and timestamps are kept. CAD thumbnails become neutral images. Deflate64 members are handled via 7z and rewritten as deflate |
| tar (incl. .tgz), gzip, `.Z` | `containers.py` | no | Same as zip. `uname`/`gname` are cleared, the gzip `FNAME` is mapped, and `.Z` is recompressed with `ncompress` |
| 7z / RAR | `containers.py` | no | Extracted with the Stage 0 guards. 7z → 7z; **RAR → zip under the same key** (RAR cannot be written). **Encrypted archives are quarantined and not shipped** |
| PDF | `pdf.py` | no | **Text:** characters are redacted (line art and images kept) and the pseudonym is redrawn at the same baseline, size and colour. **Rasters:** images go through the image scrubber; scanned pages are overlaid where text was replaced. **Metadata:** Info, XMP, annotations, form fields, links, and embedded files (recursive). **Objects:** every object and non-image stream is mapped, including `<FEFF…>` strings. Saved with garbage collection and no incremental history |
| PNG / JPEG / TIFF | `image.py` | no | **OCR** (RapidOCR): changed lines are painted with the local background and redrawn in the same ink (CJK font bundled with PyMuPDF). **Logos** (OWLv2): replaced with a neutral mark. Untouched images keep their bytes; text metadata is mapped in place |
| DWG | `dwg.py` | no | ODA round trip at the file's own version, with the DXF text writer and `THUMBNAILIMAGE` neutralised. Spike: entity counts and coordinates identical |
| empty | — | — | Copied |
| `.resume-partial` | — | — | Skipped (truncated downloads) |

## Logo pass (`deid/logo_scan.py`)
**Scope:**
- every embedded PDF image of at least 48×24 px and every standalone image, including archive members;
- 44,655 images, 38,232 unique by hash;
- blank and tiny images (shorter side under 64 px) are skipped.

**How it runs:**
- OWLv2 (`google/owlv2-base-patch16-ensemble`, fp32) at a **768** input instead of 960. That is twice as fast with the same logo-vs-drawing separation on the calibration images: logo 0.24, drawings at most 0.14.
- int8 quantisation was tested and destroys the scores (logo 0.27 → 0.03), so it is not used.
- About 7 hours on the 8 physical cores of this box (15 workers).
- Detections at 0.10 or above are cached by image hash. The rewrite applies the production threshold `DEID_LOGO_THRESHOLD` = **0.14**, chosen at the contact-sheet review. Real logos still appear in the 0.14–0.18 band, and a false positive only costs a neutral patch on a raster.
- **Drafting-callout filter** (`image.is_callout`): the dominant false positive at every score was circled section, detail and datum letters. A box is dropped when it is roughly square, monochrome, on a light background, and holds a centred Hough circle. On the review sample about 95% of what it drops are callouts.
- At 0.14 the filter keeps **5,280 boxes in 3,394 images** and drops 1,711 callouts.

**Not covered:** vector-drawn logos in PDFs (paths, not rasters) can't be detected without rendering every page. This is listed as a known limit.

## Runner (`deid/rewrite_run.py`)
1. **`plan`** computes the renamed key for every object, checks for collisions, and writes `rewrite/keys.parquet`. Result: 188,716 keys, 172,976 renamed, **0 collisions**.
2. **`run`** processes the largest blobs first. Each blob is downloaded from staging, rewritten, uploaded to its first renamed key, and server-side copied to its duplicate keys. Status parts are committed every 300 blobs, and errors are retried on rerun. Each file has a timeout of 3,600 s and is then quarantined.
3. **`report`** writes the per-format status, the quarantine list and the residual counts.

## Sample results (stratified sample: 1,012 files, every format, 1.2 GB)
| Format family | Files | Result |
|---|---|---|
| OLE2 (ipt, iam, idw, ipn, sldprt, sldasm, slddrw, slddrt, prt, asm, par, dft, prtdot) | 482 | 482 ok. Verified: stream list and sizes equal, all streams readable. About 4,800 stale sectors, 3,500 mini-sectors and 3,000 directory entries wiped |
| SolidWorks 2015+ (150-file spike set + 12 sample) | 162 | 161 ok, 1 quarantined (one section cannot be fitted to its exact size, as in the spike). 0 verification failures; every preview neutralised |
| STEP, IGES, SAT, x_t, DXF, SVG, OBJ, VRML, UNV, STL (ASCII and binary), PS, BREP, Creo neutral | 168 | All ok, same size |
| Creo, JT, Parasolid binary, U3D, CATIA, I-DEAS, native NX | 99 | All ok, same size |
| FCStd, 3MF, f3d, f3z, ODG, zip, tar, gzip, `.Z`, RAR | 117 | All ok. One f3z with a Deflate64 member rebuilt through 7z |
| 7z | 7 | 3 ok, **4 encrypted → quarantined** (by design) |
| PDF | 12 | All ok. Page and drawing counts unchanged; text, metadata and images replaced |
| DWG | 12 | All ok. Version kept |
| PNG, JPEG, TIFF | 28 | All ok |

**Recall gaps the writers surfaced, fixed in Stage 2 or the rules:**
- NX user-history lists (5 user names, 93 files);
- names inside length-prefixed strings, plurals, CamelCase and digit-glued part names;
- machine names;
- junk keys (`The VBA Project`, padding runs, a 2-letter domain, generic `TECHNISCHE UNIVERSITÄT`).

**Residual review before the full run** (manual inspection of every flag):
- Most flags were false alarms: compressed or base64 data, code identifiers, and material-schema terms.
- Four were real leaks:
  - a full name in a Creo record;
  - a short brand glued to `_LOGO` and to part numbers;
  - an NX user-history first name;
  - a brand glued to a model letter.
- **How they were fixed:** reviewer seeds (private), the glued-suffix rules and reference stems above. After the fixes, a full sample re-run had **0 hits** for all four, including inside archive members and member names.
- **Stage 2 made reproducible:** NER candidates were judged on one `any_value()` example source, so about 1% of entities flipped between builds. Now:
  - all queries are ordered;
  - a candidate is accepted when **any** of up to 20 sorted examples is human text.

  As a result, two consecutive builds are identical, and the map gained 128 rows (mostly cited authors) and lost none. Generic false positives were added to `keep_terms`.

Still open for the Stage 4 leak scan: lowercase first names that are not in the map, and people mentioned only inside binary runs (NER did not run on binary text).

## Results (full run)
The run used one c7i.4xlarge (16 vCPU, 30 GB), 14 workers, and 147,813 unique blobs (103.5 GB) behind 188,716 keys.

| | Blobs | Output keys |
|---|---|---|
| ok | 147,712 | 188,575 |
| copy (zero-byte) | 1 | 24 |
| quarantined (not written) | 100 | 117 |
| errors | 0 | — |

The output bucket was checked against the plan key by key: 188,599 objects (142.0 GB), **0 missing, 0 unexpected**. The source has 188,718 objects; the 2 not in the plan are truncated `.resume-partial` downloads, skipped by design.

- **Time:** the main pass took 5.4 h wall-clock, plus about 1.5 h for the tail described below. Worker CPU time was 75 h in total.
- **Cost:** about 7 h of on-demand instance time, roughly $5. Batch Spot would be cheaper and faster, because the tail was a handful of very large single files.
- **Biggest families by size:**

  | Family | Blobs | GB |
  |---|---|---|
  | STEP | 46,365 | 37.1 |
  | SolidWorks 2015+ | 16,635 | 11.9 |
  | binary STL | 10,425 | 10.3 |
  | Creo | 10,106 | 7.1 |
  | zip | 1,484 | 4.9 |
  | PDF | 6,884 | 4.8 |
  | OLE2 | 9,177 | 4.5 |
  | SAT | 22,098 | 3.4 |
  | FCStd | 4,698 | 3.0 |

**Quarantine (100 blobs).** None of these are written to the output. All are listed in `quarantine.json`, which is private because it contains source file names.

| Blobs | Reason |
|---|---|
| 51 | SolidWorks 2015+: a section cannot be re-deflated to its exact original size (the same failure as in the spike) |
| 22 | PDF. Malformed files: bad xref, an object that is not a stream, a broken dict, an undecodable string (18). OCR model failure on an extreme image (3). One encrypted file |
| 9 | DWG: the ODA converter segfaults, returns an error or times out, mostly on very old versions (an AC1006 file stalls when written back) |
| 7 + 4 | Encrypted zip members, and encrypted 7z/RAR (by design) |
| 4 | OLE2: a compressed stream can't be refitted to its exact size (3); a structure check failed (1) |
| 2 + 1 | A zip whose SLDDRW or f3z member is quarantined; one f3z that is not a valid zip |

**Fixed during the run:**
- **Worker OOM:** some pool workers were OOM-killed, and the pool then waited forever for their results. Two writers caused it:
  - **Residual scan:** `findall` over a 75 MB 3MF model built millions of strings at once, peaking at 9.4 GB. The scan now runs over 8 MB windows with a 1 KB overlap, giving the same hits at a 1.4 GB peak.
  - **PDF images:** scrubbed JPEG pages were re-inserted as PNG, which PyMuPDF holds as raw samples until save (about 0.4 GB per 66 MP page). They now go JPEG in, JPEG out. A 109-page scan peaks at 2.1 GB, down from more than 23 GB.
- **PyMuPDF 2.1 `embfile_upd`:** it crashes when given new content (8 PDFs with embedded files). We now rename through `embfile_upd` and write the stream with `update_stream`, and a test covers it.
- **Retrying quarantines:** `run --retry-error TEXT` re-queues quarantines whose error contains TEXT. The latest status part wins, both in the runner and in `report`.

**Still open (runner hardening):** a worker that dies by signal or OOM is still not detected, so `multiprocessing.Pool` waits forever for its result. Before the Batch definition is final, the runner should notice dead workers and record the blob as failed.

**Residual flags:** 10,420 output files still contain an original key at a word start. As in the sample, most are expected to be false alarms (compressed or encoded data, identifiers). The Stage 4 leak scan reviews them.

## Fix round after the visual spot-check
A manual review of output PDFs found leaks that no automatic check had flagged. The residual check only looks for names that are already in the map, so it cannot see names missing from it. The fixes, and how they were applied without re-running the whole corpus:

**Logos**
- **Cache-key regression:** the JPEG-in/JPEG-out memory fix had started hashing raw JPEG bytes, while the logo cache is keyed by the pixmap PNG, so every lookup missed and a miss silently meant "no logo". The cache key is now always the pixmap PNG, and a miss now runs live detection and is counted (`logo_cache_miss`); 258 misses in the re-run.
- **Whole-image logos and signatures (`deid/logo_clip.py`):** OWLv2 finds logos *inside* a scene and gives an image that *is* a logo (a title-block raster) low, partial boxes. A CLIP zero-shot classifier scores every cached image, 38,232 in 7 minutes, and the rewrite blanks the whole image when the image is 4 MP or smaller and one of these rules holds:
  - **A:** logo score ≥ 0.90;
  - **B:** logo score 0.80–0.90 with OWLv2 support ≥ 0.10;
  - **S:** signature score ≥ 0.90 and shortest side ≥ 60 px.

  These thresholds were chosen at a contact-sheet review. 837 logos and 30 handwritten signatures are blanked.
- **Deferred to Stage 4:**
  - OWLv2 boxes can be slightly tighter than the logo, leaving a thin edge visible (fix: pad about 8%, then re-run about 2k files);
  - vector-drawn logos.

**Map (Stage 2)**
- **Institutions and companies:** the dictionary-word, part-number and single-acronym filters had dropped them. A reviewed list was added as private seeds: 234 institutions and 102 companies. Standards bodies, publishers, learned societies, generic phrases and CAD tool makers are kept.
- **Seeds for org acronyms** are global (`tag: org_acronym`). An `exact: true` seed gets its own entity, so a mark like `ZORB®` never merges with the plain word `Zorb`.
- **Person initials:**
  - "D. Surname" and "D.Surname" variants of mapped names, including accounts that look like "Given Surname";
  - whole-field Initial. Surname title-block names that NER never proposed (337 forms, private `initial_names.yaml`); their bare surnames are never mapped alone.
- **Machine names:** the HOST part of `HOST\user` accounts is mapped on its own, global for host-shaped strings (letters with digits or `-`). `GLOBAL` and `ENGINEERING` are generic Windows domains and are kept.
- **Pinning:** once outputs are published the map is append-only. `map.pinned.parquet` fixes every published pseudonym, because adding persons reorders token assignment and changed 117 published pseudonyms before pinning. A new initial or host form takes its pseudonym from the pinned full name or `HOST\user` row ("Mira Talvenk" → "Ines Fintaro" gives "I. Fintaro"). Pinned rows: 0 removed, 0 changed.

**PDF text layer:** CAD-exported PDFs often have character boxes taller than the line pitch, and MuPDF removes every glyph a redaction touches. A replacement therefore erased letters of the lines above and below it. Redactions are now a thin strip through the middle of the x-height (`_glyph_core`), with a regression test. A text-damage scan (expected unmapped words vs output words) found 1,438 of the 4,126 PDFs with text replacements damaged. Only those were re-run.

**Memory:** scrubbed grey and 1-bit images keep their source mode (an RGB replacement is held raw by PyMuPDF; a 392-page bilevel scan was OOM-killed).

**Targeted re-runs** (`rewrite_run run --only FILE`), each followed by stale-key cleanup and key-by-key reconciliation:

| Round | Blobs | Why |
|---|---|---|
| 1 | 7,250 | new or widened map surfaces in text (6,371), renamed keys (593), whole-image logos/signatures (1,240), cache-key regression (16), renamed-stem references (1) |
| 2 | 1,444 | text-damaged PDFs (1,438), the "drawing no." label false positive (6). Result: 1,442 ok, 2 quarantined (OCR runtime error on a 392-page scan under load; a PDF link without its own object). Both are fixed and re-run in Stage 4 |

The renamed keys left 613 stale output objects, which were deleted. After round 1 the output bucket matched the plan (188,599 objects, 0 missing, 0 unexpected). After round 2 it is 0 missing and 5 unexpected: the earlier outputs of the 2 quarantined blobs, which the Stage 4 re-run replaces.

**Runner additions:**
- `--only` re-runs exactly the listed blobs;
- `--retry-error` re-runs quarantines whose error matches;
- `tmp_rewrite/inflight/<pid>` records the blob each worker holds, which identifies OOM-killed workers;
- status part names carry the pid, so concurrent runners never collide.

Dead-worker recovery inside `multiprocessing.Pool` is still open; a lost task makes the pool wait forever, and is recovered by re-running the unrecorded blobs.
