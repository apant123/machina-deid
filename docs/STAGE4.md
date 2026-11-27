# Stage 4: independent leak scan, triage, integrity and fixes

Stage 3's residual check only looks for names that are **already in the map**. It cannot see a name the map is missing, so this stage checks the output in other ways:
- map-free detectors over everything Stage 1 harvested;
- a direct scan of real output objects;
- logo and signature checks on rendered pages;
- integrity and reference checks per format.

Everything found was either fixed and re-run as a targeted set, or is recorded under known limits. Real names and organisations stay in the private QA folder; this document uses synthetic names only (`Mira Talvenk`, `Quorvex`, `ZORB`).

## 1. Map-free leak scan (`deid/leak_scan.py`)
**What is scanned:** every harvested string (text fields, binary text runs, OCR lines, path components). The Stage 1 harvest covers about 45M distinct values, which includes embedded metadata.

**Method:** each string is rewritten with the production map exactly as the writers do (`DeidMap.text`). Then detectors that know nothing about the map run on the result:
- email addresses;
- profile paths (`C:\Users\<x>\`, `/home/<x>/`);
- `HOST\user` accounts;
- whole-field `D. Surname` title-block names;
- labelled fields (`Drawn by: X`, `Gezeichnet: X`, ...);
- copyright holders;
- repository owners;
- every Stage 1 NER person or organisation span, including the ones Stage 2 dropped.

A hit whose text is still present after the rewrite **survived**, so it is a leak candidate. A pseudonym is new text and never counts. Output object keys are scanned directly.

**A second, field-based pass** checks every author-type metadata field: PDF/XMP author and creator, OLE author and last-saved-by, DWG `$AUTHOR`, STEP `FILE_NAME` author and organisation, and SolidWorks designer, drawn-by and checked-by properties. It flags values that come out of the rewrite unchanged and are not generic or tool names.

**Findings (after triage):**

| Class | Examples (synthetic) | Fix |
|---|---|---|
| Single first names, handles and IDs in author fields (Stage 2 dropped weak single tokens) | `Mira`, `mtalv01`, a student number | **Field-scope** seeds: whole field value only, never free text |
| People in labelled fields, copyright lines and paths | `Drawn by: Mira Talvenk`, `© 2019 Quorvex Robotics` | Global seeds |
| Organisations and universities NER found but Stage 2 dropped, or never proposed | a Fab City / university / lab name in folder names and title blocks | Global seeds (org acronyms: whole token) |
| Short org tokens next to typographic punctuation | `ZORB’s`, `zorb®`, `“ZORB”` | Rule A1 (below) |
| Vendor names inside CAD family-table instance names | `…-03<QUORVEX_FAN-0710>` | Rule A2 |
| Short letter+digit codes glued to a lower-case word | `D:\ZRB3Dlabs\` | Rule A3 |

**Measured noise:**
- Emails: of 4,419 distinct email-shaped survivors only one was a real address. The rest were SolidWorks feature and sheet references (`D1@Boss-Extrude`, `iso.slddrt@12.0`).
- Initials: almost all survivors are binary-run noise (`K.IIT`, `Q.Tke`).
- `HOST\user`: path fragments and binary noise; every real author-field account was already mapped.

**Direct scan of real outputs.** This validates the simulation: 1,468 output objects, 40 per format, harvested with the Stage 1 harvester and run through the same detectors plus original map keys (5 or more characters, word-bounded).
- Surviving detector hits were pseudonyms, tool vocabulary, CAD library folder names (`screws\\socket`) and noise.
- The original keys found were:
  - format vocabulary (bug 9);
  - a tool vendor's support address;
  - strings inside archive members (bug 10);
  - a vendor name inside `<…>` in a Parasolid binary record, which the XML guard still treats as a tag because the `<` is not glued to a preceding letter or digit.

## 2. Stage 3 residual flags (10,820 files)

| Bucket | Files | Meaning |
|---|---|---|
| Only false-alarm keys | 7,267 | a map key at the start of an ordinary word (a key like `quorv` at the start of *quorvantic*, or surnames that begin common words such as *sharpen* or *alteration*), or short acronyms inside binary noise; the rewrite rightly leaves these alone |
| Touches a real leak | 1,134 | name variants the rules did not cover (curly apostrophe, `®`, CAD instance names, glued codes, full names behind a mapped fragment). Fixed by rules A1–A3 and seeds |
| Long tail | 2,419 | 5,800+ further keys; a 40-key random sample was 100% false alarms (`ToPascalCase`, *conglomerate*, binary noise) |

## 3. Bugs found and fixed (with regression tests)
1. **One-byte length prefix (data damage).** A short key was treated as "the first word of a length-prefixed string" whenever any printable byte under 128 followed a non-printable one. So a 4-letter name matched *inside* a word in binary records (`\x00` + `h` + `inge…`). That damaged words and internal identifiers in about 5,600 Creo/DWG files (`label_…` → `l…`) and broke 0.33% of assembly→part references. The prefix byte must now equal the length of the text it prefixes.
2. **Short keys in mixed-case noise.** A 4-character key matched case-insensitively inside binary runs (`{IiT[`). Short keys now need a token-like casing (UPPER, lower, Title, or the exact casing of a map surface, such as `iZub`). Other casings are accepted only between strong separators (`\WorkSW_AleX\`).
3. **A1, typographic delimiters:** curly quotes and apostrophes, dashes, ellipsis and ® © ™ delimit short keys, in **UTF-8 only**. A first version also accepted single cp1252 bytes. Those bytes are common inside binary payloads, and the SolidWorks section verifier caught a short key matched inside tessellation data, so the cp1252 form was withdrawn before the bulk of the re-run.
4. **A2, XML guard:** a `<` glued to a preceding letter or digit is a CAD record, not markup, so the name after it is no longer protected as a tag name. Real tags and attributes stay protected.
5. **A3, mixed codes:** 5–7 character letter+digit codes need one hard edge if the other side runs into a lower-case word. They never run into more capitals.
6. **Person-like accounts** (`Mira Talvenk` typed as an account) now get initial variants (`M. Talvenk`, `M.Talvenk`), and `HOST\user` accounts give the bare `HOST`.
7. **PDF links without an object** ("bad xref" from PyMuPDF): the link update is skipped and counted. The `/URI` string is still rewritten by the object pass.
8. **Inline images in PDFs** (`BI … ID … EI`, no xref) were invisible to every image pass. See section 4.
9. **CAD format vocabulary in published map rows.** A few published rows are format vocabulary that NER mistook for organisations:
   - an AutoCAD group dictionary name;
   - AutoCAD object-enabler class names;
   - localised SolidWorks system property names (`SW-<property>`, which drawings and BOMs link to by name).

   The map stays append-only, so the rewriter now protects these token families (`ACAD_*`, `Acad*`, `*ANNOT_GROUP`, `ACIS_*`, `SW-<property>`, in 8-bit and UTF-16), the same way MFC class names were already protected.
10. **Archive selection gap in the re-run selection.** Stage 1 harvested archive members as separate virtual blobs, so a member gaining a new map entry flagged the member, not the archive the runner rewrites. Every top-level archive with a member string the current map changes is now re-run (968 archives).
11. **Short field-scope keys in packed numbers (data damage).** A short first name seeded as a whole-field value (3–4 characters) was accepted between *any* two value delimiters, in any casing. Packed floats contain such byte runs (`<qeB=`, `<QEb;`), so the name was replaced inside geometry. The SolidWorks verifier quarantined 2 files; a scan of every shipped binary and archive member found 58 more files (36 SolidWorks, 11 Creo, 9 OLE2, 2 archives) where a few float bytes had been altered without tripping a verifier. Short field keys now need token casing and either a matched delimiter pair (`".."`, `'..'`, `(..)`, `>..<`, NUL / line ends), a real length prefix (1-byte, 4-byte, MFC `FF FE FF n`), or plain text on both sides. A check over all harvested strings showed the new rule drops no genuine name (the 65 strings it rewrites differently were all binary noise or ordinary words). The 60 files were re-run.
12. **Logos stored as Stamp annotations.** Annotation appearance streams are drawn on top of the page, so blanking the page content left the logo visible in 5 PDFs (50 regions). Non-link annotations mostly inside a confirmed region are now removed before the region is blanked.
13. **Page-region scan robustness.** A page with thousands of inline images rendered every crop before classifying (12–18 GB per worker, OOM-killed; `multiprocessing.Pool` then waits forever). Crops are now rendered in batches of 32, workers have an address-space cap, a dead worker's PDF is recorded as an error instead of hanging the pool, and huge or elongated pages are rendered at a lower resolution. All 6,882 PDFs are scanned with 0 errors.

## 4. Logos and signatures on rendered pages (`deid/vector_logo_scan.py`)
**Inline images:** 107k small inline image placements in 1,289 PDFs. Each is rendered and classified with the CLIP whole-image classifier, using the same thresholds as `logo_clip` (logo ≥ 0.90; signature ≥ 0.90 with a shortest side of at least 60 px).

**Vector-drawn logos:** OWLv2 runs on the page-1 render. Boxes that do not sit on an xref image are confirmed with CLIP (≥ 0.90; 97 candidates at 0.80–0.90 are held back for review). Arc-shaped logos that look like drawing callouts need ≥ 0.95.

**Result:** 6,882 PDFs scanned, **1,156 regions confirmed in 718 PDFs**, all applied. A render check of every confirmed logo region in the shipped PDFs found the neutral mark in place; the only failures were the Stamp-annotation case (bug 12), now fixed.

**Writer:** the PDF writer blanks confirmed regions. Image pixels in the box and paths fully inside it are removed, then the neutral mark is drawn; signatures become a flat blank. Text is left to the text-layer pass, because MuPDF removes every glyph a redaction touches and logo boxes often abut title-block text. Page-1 vector hits are applied to later pages whose region renders the same, so repeated title blocks are covered.

**OWLv2 edge strips (deferred from Stage 3):** a source-vs-output contact sheet of 48 random box-blanked logos showed no readable edge strip, so the 8% padding re-run was **not** done. The residual risk is a *wordmark outside the detector box* (an icon is blanked but the company name beside it is not) when the logo sits inside a larger image. This is listed under known limits.

## 5. Integrity and references
- **Format spot-check:** 25 random outputs per format family (22 families, 550 files), output vs source:
  - PDF: page count, renders;
  - STEP: entity count and `CARTESIAN_POINT` values;
  - STL ASCII/binary: facets / triangle bytes;
  - OBJ: vertices;
  - IGES/SAT/Creo/JT/VRML/DXF: byte size;
  - OLE2: stream list and sizes;
  - SolidWorks 2015+: section layout, CRCs and structure tokens;
  - zip-based: CRC and member count;
  - images: decode and dimensions;
  - DWG: version.

  **550/550 pass.** One Fusion archive rebuilt through 7z lost its 9 directory-only entries; all files inside are present and CRC-clean.
- **References:** 400 random assemblies, 9,654 part references that resolve to a source object. After the rewrite, 99.67% resolve to an output object of the same name. Every miss was bug 1 above (a short name matched inside a word in the binary record, while the key plan correctly kept the file name), so the Stage 4 re-run restores them. With the final rules, 9,654 / 9,654 references resolve (100%; simulated from the harvested reference strings with the same map and rules the re-run used, not re-read from the bucket).

## 6. Map discipline
- The published map is pinned (`map.pinned.parquet`). Stage 4 only **added** rows: 0 pinned rows removed, 0 pinned pseudonyms changed.
- Field-scope seeds of 3 characters or fewer (initials such as `tk`) were tried and **withdrawn before publishing**: as "whole values" they also matched punctuation-delimited binary noise. A short first name that is also a month abbreviation was withdrawn for the same reason.
- New key plan: 0 collisions.

## 7. Targeted re-run
A **diff simulation** chooses the re-run set. Every distinct harvested string is rewritten with the published map and rules and with the Stage 4 map and rules; files containing a string whose rewrite differs are re-run. To that are added files whose output key changed, files referencing a renamed file stem, PDFs with confirmed page regions, and the two Stage 3 round-2 quarantines.

| Reason | Files |
|---|---|
| a string's rewrite changed (new seeds, rules A1–A3, bugs 1–2, structural protection) | 12,421 |
| output key renamed (6,429 keys; 0 collisions) | 4,458 |
| references a renamed file stem | 1,890 |
| archive with a member whose text the current map changes | 968 |
| Stage 3 round-2 quarantines (retry) | 2 |
| **total (overlapping reasons)** | **15,425** (14,483 non-PDF, 942 PDF) |

PDFs with confirmed page regions are added to this set once the page-region scan completes (5,209 of 6,884 PDFs were scanned before the stop: 1,016 regions in 596 PDFs at the review thresholds).

**Run history.** The overnight run stopped when the AWS SSO session expired (a watchdog stopped every job cleanly). After login, `resume_stage4.sh` completed the remaining steps; the page-region scan hit the OOM hang in bug 13 once, which was fixed and resumed.

| Step | Result |
|---|---|
| non-PDF re-run | 14,483 / 14,483 ok or copy (66 min) |
| page-region scan | 6,882 PDFs, 0 errors; 1,156 regions in 718 PDFs |
| PDF re-run (942 selected + PDFs with regions) | 1,500 / 1,500 ok (76 min) |
| fix round (bugs 11–12, new regions, quarantine retry) | 72 files: 69 ok, 3 quarantined (all three already failing before Stage 4) |
| stale renamed keys deleted | 6,427 |
| outputs of quarantined blobs deleted | 2 |

## Final state
- **Output bucket reconciled against the key plan: 188,599 expected, 188,599 present, 0 missing, 0 unexpected.** No object of a quarantined blob is in the bucket.
- Blob status: 147,712 ok, 1 copy, **100 quarantined** (not shipped; down from 102), 0 errors.
- The 3 quarantines left from Stage 4 work are a DWG whose converter times out, a SolidWorks drawing whose recompressed section cannot be fitted back to its exact size, and a Fusion archive that is not a valid zip. None of them shipped before Stage 4 either.

**Leak and reference state** (simulated with the final map and rules on all harvested strings):
- 9,654 / 9,654 sampled assembly→part references resolve (100%, up from 99.67%);
- no real e-mail address survives;
- the remaining labelled-field and copyright survivors are tool, licence and map attributions, plus a handful of bare first names and initials.

## 8. Known limits
- **Wordmarks outside a detector box:** possible when a logo sits inside a larger image (a scan or photo). Whole-image logos are blanked by CLIP.
- **OCR coverage:** inline-image *text* is not OCR'd. Logos and signatures in inline images are handled.
- **Cited authors:** names in bibliographies and textbook text (public authors of references) are not mapped unless they also appear in a corpus role (author, drawn-by, path).
- **Vendor and brand names in catalogue part names** (bearing, connector and profile makers) are only mapped when they appeared as companies in metadata, copyright lines or the Stage 2 gazetteer. A broader vendor gazetteer would rename many catalogue parts and is left as a policy decision.
- **Published false positives cannot be removed:** the map is append-only, so a few published rows that are ordinary vocabulary stay mapped (a turbine type, a magazine title, a German job title). They are harmless: always the same pseudonym and no geometry involved.
- **Directory-only zip entries** are dropped when an archive must be rebuilt through 7z.
- **Generic stamps** (for example "request copy, not for production") inside a logo region keep their text: the region pass leaves text to the text layer, and such stamps carry no identity.
- **100 quarantined blobs** are withheld rather than shipped with a possible leak or broken structure; they are listed in the private report.
