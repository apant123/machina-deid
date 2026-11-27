# Format spikes: native CAD rewrite feasibility

> Names, handles and e-mails in the examples below are **synthetic stand-ins**. The real values stay only in the private data directory and staging bucket (`deid.code_leak_check` enforces this).

Spike code lives in `spikes/`. Sample files are in `~/data/machina/spikes/`.
We had no CAD application available, so every check here is structural:
- the file re-parses
- checksums and sizes are consistent
- no identifying strings remain

## SolidWorks 2015+ container (`solidworks-new`; 16,686 unique blobs, 12 GB)

**Format, as reverse-engineered here.** The container is ZIP-like:
- **Local section header**: the marker `14 00 06 00 08 00`, then `u32 file_id`, `u32 crc32(uncompressed)`, `u32 compressed_len`, `u32 uncompressed_len`, `u32 name_len`, then the name. Every byte of the name is nibble-swapped (`0x72`→`0x27`). After that comes the raw-deflate payload.
  - The `crc32` field matched in 214/214 sections across 5 files.
  - Section names: `Contents/CMgr`, `Contents/Config-0-ModelHeader`, `Header2`, `docProps/core.xml`, `swXmlContents/Features`, `_MO_VERSION_*/Biography`, `Preview`, `PreviewPNG`, …
- **Central directory** (about 4 KB at the end of the file): each section's header is repeated with the same CRC and sizes, plus what appears to be an offset field.
- **First 4 bytes** (`xxxxxxxx 00000004`): *not* a CRC32 or Adler32 of the whole file, the file after the first 4 bytes, the file after the first 8 bytes, or the file header. Their meaning is unknown, but our rewrite never changes them.

**Identifying data found:**
- usernames in `C:\Users\<name>\…` paths
- `Header2` / `ModelHeader` user lists (`ktomas`, `pbellard`, …)
- `docProps/core.xml` `lastModifiedBy`
- `ReplaceRef` paths to referenced parts (Google Drive and OneDrive paths)
- `Biography` (save history)
- 95% (142/150) of the random sample contained at least one username.

**Rewrite strategy (size-preserving):**
1. Decompress → same-length replacement (ASCII + UTF-16LE).
2. Recompress to **exactly** the original compressed length:
   - try zlib configurations first;
   - then split-flush variants (branch the compressor state, insert a partial/sync flush) to shift the length residue;
   - then zopfli;
   - pad up with empty stored blocks (`00 00 00 FF FF`, 5 bytes each).
3. Patch the CRC in **every** header carrying that section's signature: the local header and the directory entry. Byte-identical duplicate sections are patched together.

File size, offsets, compressed and uncompressed sizes all stay identical. Only CRCs and payload bytes change.

**Results:** 150 random files (80 MB), 1,217 sections rewritten.
- 0 errors, 0 verification failures, 0 stale CRCs left anywhere, 0 names remaining.
- **1 section (0.08%) could not fit its exact size**: a 194-byte `ReplaceRef` with UTF-16 names. Policy: quarantine the file (about 0.7% of files) unless the directory offsets are fully decoded later, which would allow sections to grow.
- Speed: 6.4 s for 80 MB on 16 cores, so about 15 minutes for 12 GB on this box.

**Open risk:** we have not confirmed that SolidWorks/eDrawings opens a rewritten file. Next step: open about 10 rewritten files in eDrawings Viewer, which is free but Windows-only.

## OLE2 CAD (old SolidWorks, Inventor, Solid Edge)

- **Stream-level same-length replacement** with `olefile.write_stream` worked on 11/11 samples:
  - stream list and sizes unchanged;
  - property sets still parse (`last_saved_by: orvank → qelbrn`);
  - no names left in live streams.
- **Leak 1: free and slack sectors.** The leftover names sat in **FREE big sectors and unallocated mini-stream space** (stale data from earlier saves). The writer must zero free sectors, free mini-sectors, and the tail slack of each stream.
- **Leak 2: embedded zlib.** Old SolidWorks `*__ZLB` streams hold `[24-byte header incl. u32 compressed_len][zlib blob]`, and names appear inside the blobs. The same exact-size recompression works (3/3), keeping the length prefix and the Adler32 valid.
- Inventor: names in `UFRxDoc` (UTF-16) and in `RSeEmbeddings/*/Ole10Native`, which holds embedded files. Embedded payloads must be recursed into as nested files.

## Creo `#UGC` (prt/asm/drw.N; 10,106 unique blobs, 7 GB)

- Text-based: about 70–100% printable. The header is fixed-width (padded with spaces to a `\` column).
- The hex value on line 1 (e.g. `000005a6`) is **the byte offset of the end of the `#END_OF_P_OBJECT` line**, confirmed exactly in 3/3 files. It is an offset, not a checksum, so same-length edits keep it valid.
- Identifying data:
  - `#- HOST csl074`, `#- DBID RICOH` (a company name)
  - `@model_name` (equals the filename, so it is coupled to file renames: same-length pseudonyms)
  - `@usr_rev` / `@user_id` records
- Plan: same-length text replacement, then verify the offset field still points at `#END_OF_P_OBJECT`.

## DWG (694 unique blobs, 9 versions: AC1006 R10 … AC1032 2018)

**Tool: ODA File Converter 27.10** (`/usr/bin/ODAFileConverter`), chosen over LibreDWG because LibreDWG writes only R2000.
- Headless use needs `xvfb-run`: its Qt build only has the xcb plugin, not offscreen.
- Extra packages it needs: `libopengl0 libegl1 libfontconfig1 libxkbcommon-x11-0 libxcb-cursor0 xvfb`.

**Method:**
1. DWG → DXF at the **file's own version** (`AC1032`→`ACAD2018`, `AC1027`→`ACAD2013`, `AC1015`→`ACAD2000`, …; see `ODA_VERSION` in `spikes/dwg_roundtrip.py`).
2. Text replacement in the DXF. DXF is plain text, so replacements may change length; only filename references need same-length fakes.
3. DXF → DWG, same version.

**Results on 5 files (3× AC1032, 2× AC1027), `spikes/dwg_roundtrip.py`:**
- version kept 5/5;
- entity counts per type identical 5/5, for both the no-edit round trip and the de-identified one;
- coordinate multiset hash identical 5/5 (up to 44,907 coordinates);
- OBJECTS counts, xdata and 3DSOLID counts identical;
- ACIS (SAB) solid payload identical except **1 byte, the ACIS save timestamp**;
- replacements applied, including CJK text;
- 5 files in about 8 seconds.

**Findings for the writer:**
- Output is 15–70% smaller. The originals carry padding and free pages (e.g. long `IIII…` runs); no content is lost.
- **`$LASTSAVEDBY` is stamped with the OS account running ODA** (`ubuntu` here). `USER`, `LOGNAME` and `USERNAME` are ignored. Run ODA as a neutral container user (`deid`) and verify when the Docker image is built. (Creating a local system user was not approved in this session.)
- **`THUMBNAILIMAGE` survives the round trip.** The writer must blank or replace it in the DXF before converting back.
- DWG summary info (author etc.) does not appear in the DXF, so the round trip drops it. Acceptable for de-identification.
- Identifying data seen: title-block TEXT/MTEXT/ATTRIB, layer and block names, a plotter path with a username (`C:\Documents and Settings\<user>\…`).
- One object labelled `.dwg` has a non-DWG header; route it to "unknown".

## Archives (Stage 0c, `deid/unpack.py`)

Guards, all tested:
- zip-slip and absolute paths;
- symlinks, hardlinks and devices are skipped, never created;
- bomb caps on per-member size, total size, ratio and member count;
- nesting depth up to 3;
- GBK/CP437 zip filenames decoded.

Final run (`python -m deid.unpack`, then `--retry-failed`):
- 2,367 unpaired archive blobs at depth 0, plus 101 nested.
- Outcome: **2,447 ok, 10 recovered, 11 quarantined (all encrypted)**, 0 errors.
- 64,590 members (6.5 GB; 23,655 unique) under `s3://…raw…/_unpacked/<archive_sha256>/`.
- Index files: `_manifest/archives.parquet` and `_manifest/members.parquet`.
- The members include formats not in the original R2 listing:
  - **KiCad** (`kicad_pcb/sch/sym/mod/pro`, about 5.7k): text, with title-block company and author fields
  - Gerber/drill files (`.gbr`, `.drl`)
  - 41k NIST `.gr1` text files
  - geojson, C headers
  These go into the Stage 1 text harvesters.
- Fixes made during the run:
  - zip names that come from a Unicode-path extra field were mis-decoded;
  - RAR needs Debian's separate `7zip-rar` codec package;
  - member uploads are now parallel (a 41k-member `.tgz` had taken 20 minutes).

Findings:
- **Corrupt-at-source gzip** (1990s NIST `.arc.gz`, CRC and length mismatch): raw-deflate recovery, `status=recovered`, so the contents are still de-identified.
- **Encrypted archives** (7 zip blobs incl. `hardgate/*.zip`; 4 header-encrypted `retroguide/.../*.7z`): quarantined. The contents can't be read, but the central-directory filenames are plaintext, so the output policy must decide whether to drop these files or rebuild them.

## eDrawings validation and the File 10 fix

**v1 result (Windows Server 2022, eDrawings):** 12/13 passed. Files 01–09 (SW 2015+) and 11–13 (OLE2) passed; **File 10 failed** with "Unhandled parsing exception".

**Root cause.** The spike used a raw *substring* replace, and extracted the generic Windows account `User` as if it were a person. That rewrote SolidWorks' MFC serialization class tags:
- `moLengthUserUnits_c` → `moLengthjkztUnits_c`
- `moAngleUserUnits_c`, `moNumberUserUnits_c`, `moFloatNumberUserUnits_c`, `moFrequencyUserUnits_c`
- `uiUserModelEnv_c`

The loader resolves classes by name, so the file failed to parse. The same bug also produced `C:\Users\` → `C:\jkzts\` and corrupted the XML property-set name `UserDefinedProperties`.

**Impact audit of the v1 logic on all 150 sample files:**
- 28 files (19%) had matches inside larger tokens; 2 of them hit class names.
- QA files 01, 02, 05 and 07 were affected but happened to open.
- Generic accounts were extracted as people in 7 files (`User` ×2, `Administrator` ×4, `Admin`).

**Fix: `deid/replace.py` (`Replacer`).** It is shared by all native writers. Tests: `tests/test_replace.py`, 17 cases.
- **Hard guards.** MFC class names (`[a-z]{2}[A-Z]…_c`, ASCII and UTF-16) and XML tag and attribute *names* are never modified.
- **Word boundaries.** A single letter-valued byte directly after binary is treated as a length prefix (e.g. `0x4A`='J'). A naive boundary test would leak names stored as `\x4Aname`.
- **Generic-account stoplist**: User, Admin, Administrator, Owner, Guest, PC, Usuario, … These are never pseudonymised.
- **Case-insensitive matching for ASCII keys**, with the matched case copied onto the pseudonym (`PARTSVAULT`, `AleX`).
- **Short keys (≤ 4 ASCII characters) are replaced only as whole strings**: 1-byte CString length = key length; MFC Unicode `FF FE FF <len>`; OLE `VT_LPSTR`/`VT_LPWSTR` (u32 length including NUL); or delimited text. This prevents corrupting random geometry bytes that happen to spell `rje`.
- **Bug fixed:** UTF-16 matches at odd offsets were silently skipped. That is exactly where SolidWorks CStrings start, so names leaked.
- **Bug fixed:** short names in OLE property sets (`Pim` as Last Saved By) were not recognised.
- **Longest key first.** CJK keys use substring matching (no word boundaries in CJK text).
- **`residual()`** reports original names still present inside real text runs, ignoring binary noise, so a file can be flagged for review or quarantine.

**Verification (`spike/sw_rewrite.verify`)** now includes `structure_preserved`: every section's MFC class names and XML names must be byte-identical. Applied to the v1 bundle, it **reproduces the eDrawings result exactly**: 01–09 preserved, File 10 flagged, with the 4 corrupted class tags named.

**After the fix, 150-file batch:**
- 0 structure breaks, 0 verification failures;
- 28,082 replacements (v1: 26,062; the difference is occurrences v1 missed);
- 1 exact-size quarantine (unchanged);
- 3 residual flags:
  - `teodo` ⊂ `Teodorico_Vallemo` + student ID: a real name. Needs Stage 2 prefix expansion and NER.
  - `reprap` ⊂ server `\\SRVREPRAP\`: flagged correctly.
  - `RJE` in display-list binary: a benign false flag.

**v2 bundle:** all 13 files rebuilt with `spikes/build_qa_bundle.py`; 13/13 verified; 0 residual flags.

**Stage 2 items surfaced here:**
- truncated 5-character Windows profile names (`quorv` → `quorvandelibast`, `teodo` → `Teodorico_Vallemo`);
- organisation names inside paths (`UC Westfield`);
- student or employee IDs used as folder names (`9900417`, `17075`);
- one entity seen in several cases (`Ravik`/`ravik`) must map to one pseudonym (handled by case-insensitive matching).
