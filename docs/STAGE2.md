# Stage 2: entity resolution and the pseudonym map

> Names, handles and e-mails in the examples below are **synthetic stand-ins**. The real values stay only in the private data directory and staging bucket (`deid.code_leak_check` enforces this).

**Goal:** a single global map from each identifying *surface string* to a *pseudonym*. Stage 3 applies it to file contents, embedded references and object keys. The map is PII, so it lives only in `~/data/machina/entities/` and the private staging bucket, never in the code bucket.

**Policy decision (2026-11-26):**
- **Kept:** CAD and software tool names. They describe the file format, not the author.
- **Replaced:** part-catalogue, supplier and vendor names, consistently, the same way as any other organisation.

The lists are in `config/entity_policy.yaml`. That file holds only public names, no harvested values.

Code:
- `deid/ner_run.py`: NER input and the GLiNER pass.
- `deid/entities.py`: sources, rules, linking and rendering.
- `deid/pseudonym.py`: token pseudonyms.
- `deid/entity_scan.py`: corpus hit counts and pruning.
- `deid/spotcheck.py`: the review report.
- `deid/code_leak_check.py`: the pre-upload gate (no original surface may appear in the repo).
- `tests/test_entities.py`: the tests.

## Sources, in order of evidence strength
| Source | What | Notes |
|---|---|---|
| owner | Repo owners from the object keys | Every owner is an entity. Distinctive parts of org-style owners (`ZORBLAT` of `ZORBLAT-Inc`) and `First-Last` owners become entities too |
| field | Author / company / last-saved-by / creator / `USER` / `SITE` / copyright values | Parsed: `HOST\user (HOST)`, e-mails, URLs, phones, `©`/years, multi-name lists. Tool stamps are kept (`SOLIDWORKS 2018 SP4.0`, `Open CASCADE STEP translator 7.4`) |
| regex | All 45 M values, including binary text runs | **Profile folders**: drive-rooted `X:\Users\<x>\`, or `/home/<x>/` and `/Users/<x>/` at a path start. **E-mails**: real TLD, plausible shape. **URL hosts**: registrable domain, TLD and generic sub-labels kept. **Others**: `OneDrive - <Org>` tenants, phone numbers next to Tel/Phone |
| vendor | Gazetteer of catalogues and suppliers | Public list, so recall for vendors is deterministic |
| ner | GLiNER multilingual (`urchade/gliner_multi-v2.1`, Apache-2.0) on **local CPU** | Labels: person / company / organization / university / brand. Runs on human text only (see below) |

**NER input:**
- Unique text-like strings: metadata, PDF/SVG/DXF/DWG text, labels, STEP product names, file and directory names, OCR, plus source code, but only its copyright/author lines.
- Strings that differ only in digits are de-duplicated (`Part12` ≈ `Part13`).
- Strings are prefiltered to those containing a *candidate proper-noun token*: a capitalised non-dictionary word, a personal name, CJK, or `@`.
- Strings are split into pieces of at most 120 characters.
- Binary text runs (SolidWorks, Creo, OLE, exe) are **not** NER'd. They are huge and noisy, and are covered by the map itself plus the regex pass.

## Rules (precision)
- **Kept by policy:**
  - tool names, including a tool name followed only by version or product descriptors;
  - standards, licences, fonts and file-format words;
  - generic accounts (`Administrator`, `Operator`, `Author`, `微软用户` "Microsoft user", `您的姓名` "your name", …);
  - infrastructure domains (`w3.org`, `openxmlformats.org`, `digicert.com`, …);
  - generic mail domains: the local part is replaced, `@gmail.com` is kept.
- **Dictionary:**
  - GCIDE headwords, including plural, -es and -ies forms;
  - frequent English words (wordfreq zipf ≥ 3.5) and frequent fr/es/de/it/pt words (≥ 3.3);
  - excluding names in the Faker first/last-name pools, so `demo` counts as a word and `zephyn` does not.
- **Weak single tokens** (a dictionary word, 3 characters or fewer, or digits only) need strong evidence: a profile folder, a repo owner, a `HOST\user` value, a URL owner, an org-field acronym, or two independent sources. `Toolbox`, `Name` and `CAD` as author values are therefore dropped.
- **NER spans are accepted when:**
  - **Persons**: at least 2 capitalised tokens, at least one of them non-dictionary (or score ≥ 0.85). A single first name needs a non-dictionary token and score ≥ 0.85.
  - **Orgs, brands and universities**: an org suffix, or non-dictionary tokens with score ≥ 0.6.
- **NER spans are rejected for:**
  - part numbers or codes (a token with 3 or more digits);
  - all-caps code chains (`HDWR-HYDR-MASTFIT`);
  - CamelCase identifiers (`BaseRoulante`, `ThdSchPat`);
  - acronyms of 5 characters or fewer without surrounding context;
  - single-token "universities", which GLiNER assigns to unknown codes, unless score ≥ 0.95 and the token is a capitalised word (`Hollowmere`);
  - spans taken from non-text sources, or from code (`#define`, `::`, `0x..`, `;`, Doxygen `@brief`), and identifiers with an all-caps `_` part (`DMA_Channel`);
  - a single all-caps token found by NER alone (MCU register names such as `USART`); it needs a non-NER source;
  - person spans that are initials only (`D. S`), a CamelCase single token, or short all-caps tokens;
  - CJK persons that don't start with a surname character, or that are frequent words (job titles: `工程师` "engineer", `讲师` "lecturer");
  - CJK orgs whose remainder, once the org word is stripped, is generic (`机械学院` "school of mechanical engineering", `下水道局` "sewerage bureau").
- **Kept as tool makers:** a tool name followed by a corporate suffix (`SolidWorks Corporation`, `PTC Inc`, `Parametric Technology Corporation`, `Dassault Systèmes`).
- **Reviewer seeds:** names the reviewer adds after the spot-check go in `ENT_DIR/manual_seeds.yaml`, and the recall probes in `ENT_DIR/probes.yaml`. Both are private, because they contain real names.
- **Aliases:**
  - 5-character Windows profile truncations: `quorv` → follows `quorvandelibast`'s pseudonym prefix.
  - 8.3 names: `TENHOR~1` → `QAXEWU~1`, with `~N` and the extension kept.
  - Handles derived from a person's name: `talvenkm` = Talvenk+M → `fintaroq`, from that person's pseudonym.
- **Cross-script linking:** CJK ↔ Latin twins are linked by pinyin (`白鹤` ↔ `BaiHeBot`). The link is recorded for review only, and each script keeps its own same-width pseudonym.

## Pseudonyms (`deid/pseudonym.py`)
- **Per token, deterministic:** HMAC-SHA256(seed, class|token). The seed is `DEID_SEED` in `.env`, owner-only.
  - One real token gets one fake token everywhere, so `Vallemo`, `VALLEMO` and `Teodorico_Vallemo` all agree.
- **Same width in every encoding:** same characters, same UTF-8 / UTF-16 / cp1252 / GBK widths, and the case pattern is copied. This is required by the binary CAD writers and the object-key renames (see `deid/replace.py`).
  - Accented letters → accented letters with the same encoding profile.
  - Cyrillic and Greek → the same script.
  - CJK → GB2312 level-1 characters; people get a common surname character plus given-name characters.
- **Classes:**
  - Persons use first/last-name pools of the exact length (Faker, 19 locales).
  - Single first names used as accounts use the first-name pool (`Zephyn` → `Mirelle`).
  - Other words use pronounceable synthetic words; tokens of 4 characters or fewer use acronym letters.
  - Digits are kept, except in phone numbers and numeric IDs.
- **Generic org words are kept:** `Universidad … de …`, `GmbH`, `Lab`, `Supply Company`, `大学`, `有限公司`.
- **Separator and order variants** of multi-token names reuse the same tokens:
  - `Mira Talvenk` / `Mira_Talvenk` / `Talvenk, Mira` / `M. Talvenk` / `MTalvenk`;
  - rare surnames on their own (`Talvenk`).
  - Variants never seen in the corpus are pruned by the scan.

## Scopes (how Stage 3 applies a row)
- **`global`**: word-bounded match anywhere, using `deid.replace.Replacer` with its structural protections. Keys of 4 characters or fewer are only replaced as whole delimited strings.
- **`field`**: replaced only as
  - a complete metadata field value,
  - the user-name component of a profile path (`C:\Users\<x>\`, `/home/<x>/`), or
  - a repo-owner object-key component.

- **`path`**: keys of 3 characters or fewer are replaced only as a profile-folder user name or a whole object-key component, never as a field value (so `No` and `id` stay).

  The `field` scope is for weak single tokens (`Infinity`, `rje`, `81452`). It prevents, for example, the word "space" being rewritten everywhere.

## Outputs (`~/data/machina/entities/`, PII)
| File | Content |
|---|---|
| `entities.parquet` | One row per entity: key, type, all surfaces, sources, evidence, alias / cross-script link |
| `map_candidates.parquet` | Every surface → pseudonym row, including generated variants |
| `map.parquet` | **The map**: rows observed in a source or hit by the corpus scan, with scope, hit counts, where the hits are (content / path / ocr) and example contexts |
| `scan.parquet` | All candidates with their hit counts |
| `decisions.parquet` | Every candidate that was kept by policy or dropped, with the reason (recall review) |
| `spotcheck.md` | The review report |

## Results (2026-11-26)

**NER pass:**
- 1,079,215 digit-templated inputs, 484,711 prefiltered pieces (23 M characters).
- Ran on 7 processes × 2 threads; the final run took 168 minutes.
- No data left the instance.

**Map:**
- **13,064 entities.** Persons 6,187, accounts 2,433, orgs 2,426, domains 1,373, e-mails 433, vendors 129, brands 83.
- **14,061 map rows**, of which 13,937 have corpus hits.
- **Scopes:** 13,797 global, 163 field, 101 path.
- **Width check:** 14,061/14,061 same width in UTF-8, UTF-16, cp1252 and GBK.

**Spot-check:**
- The review found false positives, which were fixed:
  - MCU register names from NER;
  - CAD makers' names;
  - CJK job titles and bureau names;
  - tiny initials variants (`NO`, `ID`);
  - surname-alone variants of NER-only persons;
  - an `ß` case-fold collision.
- Recall gaps were fixed with reviewer seeds: a product name, a brand in Latin and CJK script, and a university name split by OCR.

**Pre-upload gate:** `python -m deid.code_leak_check` scans this repo for any distinctive original surface from the map. The code bucket upload is blocked until it reports `no leaks`. Examples in docs and tests are being replaced with synthetic ones.
