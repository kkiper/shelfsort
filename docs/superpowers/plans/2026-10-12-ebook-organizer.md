# Ebook Organizer (shelfsort) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Unraid Docker web app that deduplicates, identifies, sorts (subject-first), and writes metadata for thousands of ebooks/magazines/plans, with a human review queue and full undo.

**Architecture:** A Python package with one module per pipeline stage (scan → dedupe → identify → classify → group → plan → apply), all state in SQLite. External tools (Calibre CLI, Tesseract, Ollama) are called only through one timeout-guarded wrapper module. A FastAPI + Jinja + htmx UI drives a single background worker.

**Tech Stack:** Python 3.12, sqlite3 (stdlib), PyMuPDF, isbnlib, rapidfuzz, pytesseract, httpx, FastAPI, Jinja2, htmx, uvicorn, pytest. Calibre CLI (`ebook-meta`, `fetch-ebook-metadata`) and Tesseract are installed in the image.

**Spec:** `docs/superpowers/specs/2026-10-12-ebook-organizer-design.md`. Section references below (§n) point to it.

## Global Constraints

- The tool never deletes or overwrites a file. "Remove" means moving it to `<source root>/_quarantine/<relative path>`. Only the "Empty quarantine" action deletes, and it requires confirmation.
- Stages before Apply never modify anything under `/sources`.
- Every apply operation is journaled *before* it runs and confirmed *after* (§8).
- Defaults: web port `8585`; confidence threshold `0.8`; Plans & Articles page limit `40`; fuzzy match threshold `90` (rapidfuzz `token_set_ratio`); ISBN scan = first `10` + last `5` pages; OPF sidecar for all files = **on**; container user `99:100`.
- Handled extensions: `pdf epub mobi azw azw3 lit fb2 djvu cbz cbr jpg jpeg png`. All other files are `ignored`.
- Characters not allowed in names: `< > : " | ? *`, control chars, and a trailing dot or space. Each path component is at most 200 characters.
- Kind folder names are exactly `Books`, `Magazines`, `Plans & Articles`.
- AI is only reached through the optional Ollama resolver. It is disabled by default and no cloud endpoints are used.
- Tests that need Calibre/Tesseract/Ollama are marked `@pytest.mark.tools` and run inside the container (`docker compose run --rm app pytest -m tools`). Plain `pytest -m "not tools"` must pass on any machine.

## Review Focus

1. **Non-Latin and odd filenames** (e.g. `Ryusuke Sakaino [境野 隆祐]`, a name made entirely of illegal characters like `<<<.pdf`): CJK must survive sanitizing, and a component that sanitizes to empty must fall back to `untitled`. Test added in Task 2.
2. **Two planned files with the same target path in one plan** (not just a clash with a file already on disk): the second gets ` (2)` and a review item. Test added in Task 13.
3. **Case-only collisions on SMB** (`Woodworking/` vs `woodworking/`, `Book.pdf` vs `book.PDF`): treat collision checks as case-insensitive. Test added in Task 13.
4. **A file moved or deleted by the user between plan and apply:** that entry is skipped and marked `review` ("source changed"), and the rest of the batch continues. Test added in Task 15.
5. **Quarantine target already exists** (two duplicates with the same relative path from earlier runs): suffix it, never overwrite. Test added in Task 15.

---

## File Structure

```
pyproject.toml                 deps, pytest config (markers: tools)
Dockerfile, docker-compose.yml image + local dev
unraid/shelfsort.xml           Unraid template
src/shelfsort/
  models.py      dataclasses + status/kind enums
  db.py          schema, connect(), small typed query helpers
  settings.py    Settings dataclass, load/save to DB, defaults
  names.py       sanitize, normalize, matching key
  tools.py       subprocess wrapper with timeout; calibre/tesseract calls
  pdftext.py     PyMuPDF page text, page count, render page → PNG
  scan.py        walk sources, hash cache
  dedupe.py      exact duplicate groups + keeper rule
  identify/
    __init__.py  identify_file(): resolver chain
    annas.py     Anna's Archive filename parser
    embedded.py  embedded metadata resolver
    isbn.py      ISBN extract/validate + lookup resolver
    magazine.py  cover OCR resolver
    ollama.py    optional AI resolver
  classify.py    subject + kind
  groups.py      variants / near-copies / possible matches
  plan.py        target paths, collisions
  writemeta.py   embedded write, OPF sidecar, originals backup
  mounts.py      source mounted/writable checks
  apply.py       batches, journal, undo, recover, empty quarantine
  pipeline.py    run_scan_pipeline(): orchestrates stages
  jobs.py        single background worker + progress
  web/app.py, web/routes_*.py, web/templates/*.html, web/static/htmx.min.js
tests/            mirrors modules; tests/fixtures/ builders
```

---

### Task 1: Project skeleton, models, database

**Files:**
- Create: `pyproject.toml`, `src/shelfsort/__init__.py`, `src/shelfsort/models.py`, `src/shelfsort/db.py`, `src/shelfsort/settings.py`
- Test: `tests/test_db.py`, `tests/test_settings.py`

**Interfaces:**
- Produces:
  - `models.Kind` (str Enum): `BOOK="Books"`, `MAGAZINE="Magazines"`, `PLAN="Plans & Articles"`
  - `models.FileStatus` (str Enum): `new, ignored, identified, review, planned, organized, quarantined, unreadable, retry`
  - `@dataclass FileRecord(id:int|None, source:str, relpath:str, size:int, mtime:float, sha256:str|None, ext:str, pages:int|None, status:FileStatus)`
  - `@dataclass Identity(title:str|None=None, authors:list[str]=[], year:int|None=None, publisher:str|None=None, isbn:str|None=None, magazine:str|None=None, issue_date:str|None=None  # "YYYY-MM", issue_number:int|None=None, cover_story:str|None=None, method:str="", confidence:float=0.0)`
  - `@dataclass PlanEntry(file_id:int, action:str  # move|quarantine|rename, target_relpath:str, write_meta:bool, kind:Kind|None, subject:str|None)`
  - `db.connect(path: str) -> sqlite3.Connection` (creates schema if missing; `row_factory=sqlite3.Row`; WAL mode)
  - Tables: `files, identities(file_id, method, json, confidence), groups(id, type, key), group_members(group_id, file_id, decision), review_items(id, reason, file_id, group_id, resolved, decision_json), plan_entries, batches(id, scope, state, created), journal(id, batch_id, seq, op, src, dst, state), lookup_cache(key, json, fetched), settings(key, json)`
  - `settings.Settings` dataclass with defaults from Global Constraints plus `sources: dict[str,str]` (name → container path), `subject_map: dict[str,str]`, `vague_folders: list[str]` (default `["downloads","misc","new folder","unsorted","temp","inbox"]`), `keyword_rules: dict[str,list[str]]`, `magazines: list[str]`, `ollama_enabled=False`, `ollama_url=""`, `ollama_model="llava"`; `load(conn) -> Settings`, `save(conn, s) -> None`

- [ ] **Step 1: Write failing tests**

```python
def test_connect_creates_all_tables(tmp_path):
    conn = db.connect(str(tmp_path / "s.db"))
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"files","identities","groups","group_members","review_items",
            "plan_entries","batches","journal","lookup_cache","settings"} <= names

def test_settings_defaults_and_roundtrip(tmp_path):
    conn = db.connect(str(tmp_path / "s.db"))
    s = settings.load(conn)
    assert (s.port, s.confidence, s.plan_page_limit, s.fuzzy_threshold) == (8585, 0.8, 40, 90)
    assert s.sidecar_all is True and s.ollama_enabled is False
    s.magazines = ["American Woodworker"]; settings.save(conn, s)
    assert settings.load(conn).magazines == ["American Woodworker"]
```

- [ ] **Step 2: Run** `pytest tests/test_db.py tests/test_settings.py -v`. Expected: FAIL (import error).
- [ ] **Step 3: Implement** models, `db.connect`, and settings. Settings are stored as a single JSON row `key="settings"`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: project skeleton, models, sqlite schema, settings`

---

### Task 2: Name utilities

**Files:** Create `src/shelfsort/names.py`; Test `tests/test_names.py`

**Interfaces:**
- Produces:
  - `sanitize_component(s: str, max_len: int = 200) -> str`: replace not-allowed characters with nothing, collapse whitespace, strip a trailing dot/space, NFC-normalize, keep non-Latin characters. Empty result → `"untitled"`. Truncate preserving the extension if the input has one.
  - `normalize_title(s: str) -> str`: lowercase, ASCII-fold Latin diacritics (keep non-Latin as-is), strip punctuation, drop noise words `{"the","a","an","edition","ed","vol","volume","annas","archive"}`, drop the `-- Anna's Archive` tail, collapse spaces.
  - `matching_key(ident: Identity) -> str | None`: magazine → `f"mag|{normalize_title(magazine)}|{issue_date}"` (None if either is missing); else → `f"book|{normalize_title(title)}|{normalize_title(authors[0]) if authors else ''}"` (None if no title).

- [ ] **Step 1: Write failing tests**

```python
def test_sanitize_strips_illegal_and_trailing():
    assert sanitize_component("459331368<.pdf") == "459331368.pdf"
    assert sanitize_component("Title. ") == "Title"

def test_sanitize_keeps_cjk_and_falls_back():            # Review Focus 1
    assert "境野 隆祐" in sanitize_component("Ryusuke Sakaino [境野 隆祐]")
    assert sanitize_component("<<<") == "untitled"
    assert sanitize_component("<<<.pdf") == "untitled.pdf"

def test_sanitize_truncates_keeping_ext():
    out = sanitize_component("x" * 300 + ".epub")
    assert len(out) == 200 and out.endswith(".epub")

def test_normalize_and_key():
    assert normalize_title("The Router Table, 2nd Edition!") == "router table 2nd"
    a = Identity(title="American Woodworker Router Table", authors=[])
    b = Identity(title="american woodworker router table")
    assert matching_key(a) == matching_key(b)
    m = Identity(magazine="Wood", issue_date="2002-04")
    assert matching_key(m) == "mag|wood|2002-04"
```

- [ ] **Step 2: Run** `pytest tests/test_names.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the three functions as specified above.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: filename sanitizing and title normalization`

---

### Task 3: ISBN extraction and validation

**Files:** Create `src/shelfsort/identify/isbn.py` (extraction part); Test `tests/identify/test_isbn_extract.py`

**Interfaces:**
- Produces:
  - `valid_isbn(s: str) -> bool`: strips hyphens and spaces; checks the ISBN-10/13 checksum (use `isbnlib.is_isbn10/is_isbn13`).
  - `find_isbns(text: str) -> list[str]`: canonical (digits/X only), deduped, in order of appearance, checksum-valid only. Matches with or without an `ISBN` prefix, with hyphens or spaces. Prefers 13-digit over the equivalent 10-digit (convert with `isbnlib.to_isbn13` to dedupe).

- [ ] **Step 1: Write failing tests**

```python
def test_find_isbns_variants():
    text = "ISBN 978-1-4629-2322-9 ... also ISBN-10: 1462923224 and junk 1234567890"
    assert find_isbns(text) == ["9781462923229"]

def test_valid_isbn():
    assert valid_isbn("1462923224") and not valid_isbn("1462923225")
```

- [ ] **Step 2: Run** `pytest tests/identify/test_isbn_extract.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** both functions.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: ISBN extraction and validation`

---

### Task 4: Anna's Archive filename parser

**Files:** Create `src/shelfsort/identify/annas.py`; Test `tests/identify/test_annas.py`

**Interfaces:**
- Consumes: `Identity`, `isbn.valid_isbn` (Task 3)
- Produces: `parse_annas(filename: str) -> Identity | None`. Returns None unless the stem contains ` -- ` and ends with `Anna's Archive` (or `Anna’s Archive`, with a curly apostrophe). Fields are positional after splitting on ` -- `: title, author, then remaining segments identified by shape. A 4-digit year 1450–2099 → year. A segment of digits/X/hyphens that passes `valid_isbn` → isbn. 32 hex chars → ignored (MD5). Anything else non-empty → publisher (first one only). Author `Unknown`/`Desconocido`/`Anonymous` → `authors=[]`. Title: replace `_ ` with `: `. `method="annas"`, `confidence=0.9`, or `0.95` if an ISBN is present.

- [ ] **Step 1: Write failing tests** using real names from the spec §2:

```python
def test_full_annas_name():
    i = parse_annas("A Beginner's Guide to House Plants_ Creating Beautiful and -- Ryusuke Sakaino "
                    "[境野 隆祐] -- 1, 2022 -- Tuttle Publishing -- 1462923224 -- "
                    "34a156df83fd6ad7a28c0acb1803ad1f -- Anna's Archive.pdf")
    assert i.title.startswith("A Beginner's Guide to House Plants: Creating")
    assert i.authors == ["Ryusuke Sakaino [境野 隆祐]"]
    assert i.year == 2022 and i.publisher == "Tuttle Publishing"
    assert i.isbn == "1462923224" and i.confidence == 0.95

def test_unknown_author_and_no_isbn():
    i = parse_annas("American Woodworker Router Table -- Desconocido -- "
                    "1aa655cf2e3d698418bca2d8523a2531 -- Anna's Archive.epub")
    assert i.authors == [] and i.isbn is None and i.confidence == 0.9

def test_non_annas_returns_none():
    assert parse_annas("All-weather-Morris-Chair3.pdf") is None
```

Note `1, 2022`: the year is extracted from a segment *containing* a valid 4-digit year when that segment is otherwise only digits, commas, and spaces.

- [ ] **Step 2: Run** `pytest tests/identify/test_annas.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `parse_annas`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: Anna's Archive filename parser`

---

### Task 5: Scan with hash cache

**Files:** Create `src/shelfsort/scan.py`; Test `tests/test_scan.py`

**Interfaces:**
- Consumes: `db.connect`, `FileRecord`, `Settings.sources`
- Produces: `scan_sources(conn, sources: dict[str,str], progress: Callable[[int],None] | None = None) -> ScanResult` with `@dataclass ScanResult(added:int, updated:int, unchanged:int, missing:int)`. Behavior:
  - Skips any directory named `_quarantine` at the source root.
  - Unhandled extensions → `status=ignored`, no hash.
  - Rehash only when size or mtime changed. Hashing is SHA-256 in 1 MiB chunks.
  - Files in the DB but missing on disk → status unchanged, counted in `missing`, never removed from the DB.
  - `relpath` uses forward slashes, relative to the source root.

- [ ] **Step 1: Write failing tests**

```python
def test_scan_hashes_and_ignores(tmp_path, conn):
    (tmp_path/"a.pdf").write_bytes(b"x"); (tmp_path/"notes.txt").write_text("y")
    (tmp_path/"_quarantine").mkdir(); (tmp_path/"_quarantine"/"old.pdf").write_bytes(b"z")
    r = scan_sources(conn, {"remote": str(tmp_path)})
    rows = {row["relpath"]: row for row in conn.execute("select * from files")}
    assert set(rows) == {"a.pdf", "notes.txt"}
    assert rows["notes.txt"]["status"] == "ignored" and rows["a.pdf"]["sha256"]

def test_rescan_skips_unchanged(tmp_path, conn, monkeypatch):
    (tmp_path/"a.pdf").write_bytes(b"x"); scan_sources(conn, {"r": str(tmp_path)})
    calls = []; monkeypatch.setattr(scan, "sha256_file", lambda p: calls.append(p) or "h")
    r = scan_sources(conn, {"r": str(tmp_path)})
    assert calls == [] and r.unchanged == 1
```

A `conn` fixture (tmp DB) goes in `tests/conftest.py`.

- [ ] **Step 2: Run** `pytest tests/test_scan.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `scan_sources` and a module-level `sha256_file(path) -> str`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: source scanning with hash cache`

---

### Task 6: Exact duplicates

**Files:** Create `src/shelfsort/dedupe.py`; Test `tests/test_dedupe.py`

**Interfaces:**
- Produces: `find_exact_duplicates(conn, local_source: str = "local", vague_folders: list[str] = ...) -> list[tuple[int, list[int]]]` returning `(keeper_file_id, [dup_ids])`. It writes `groups(type="exact")` + `group_members(decision="keep"|"quarantine")`. Keeper ordering (spec §5.2): (1) source == local_source; (2) stem is not purely digits/punct; (3) top folder not in vague list; (4) shortest relpath; (5) lowest id as the tiebreak.

- [ ] **Step 1: Write failing tests**

```python
def test_keeper_prefers_local_then_descriptive(conn, add_file):
    a = add_file("remote", "Woodworking/011167038.pdf", sha="h1")
    b = add_file("remote", "Woodworking/Router Table.pdf", sha="h1")
    c = add_file("local", "x/011167038.pdf", sha="h1")
    assert find_exact_duplicates(conn) == [(c, [a, b])]
    conn.execute("delete from groups"); conn.execute("delete from group_members")
    conn.execute("delete from files where id=?", (c,))
    assert find_exact_duplicates(conn) == [(b, [a])]
```

`add_file` fixture in conftest inserts a `FileRecord` and returns the id.

- [ ] **Step 2: Run** `pytest tests/test_dedupe.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Group by `sha256` with count > 1, excluding `ignored` files.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: exact duplicate detection with keeper rule`

---

### Task 7: External tool wrapper, PDF text, embedded metadata resolver

**Files:** Create `src/shelfsort/tools.py`, `src/shelfsort/pdftext.py`, `src/shelfsort/identify/embedded.py`; Test `tests/test_tools.py`, `tests/identify/test_embedded.py`

**Interfaces:**
- Produces:
  - `class ToolError(Exception)`, `class ToolTimeout(ToolError)`
  - `run_tool(args: list[str], timeout: float = 60) -> str`: returns stdout. Raises `ToolTimeout` on timeout (kills the process) and `ToolError` on a nonzero exit, with stderr included.
  - `ebook_meta_read(path: str) -> dict[str,str]`: parses `ebook-meta <path>` "Key : value" output (keys lowercased: title, author(s), publisher, published, identifiers, comments).
  - `pdftext.page_count(path) -> int`, `pdftext.page_text(path, pages: Iterable[int]) -> str`, `pdftext.render_page_png(path, page: int, dpi: int = 200) -> bytes`. Raises `ToolError` on corrupt files.
  - `embedded.resolve(path: str, ext: str) -> Identity | None`: maps `ebook_meta_read` output; discards junk titles matching `^(microsoft word - |untitled|unknown|scan|document\d*$|adobe )` (case-insensitive) or titles equal to the file stem. Extracts an ISBN from `identifiers` (`isbn:...`). `method="embedded"`; confidence 0.85 with title+author, 0.7 with title only. With an ISBN present, `isbn` is set but confidence is unchanged (Task 8 enriches it).

- [ ] **Step 1: Write failing tests**

```python
def test_run_tool_timeout():
    with pytest.raises(ToolTimeout):
        run_tool([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)

def test_run_tool_nonzero():
    with pytest.raises(ToolError, match="boom"):
        run_tool([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])

def test_embedded_discards_junk(monkeypatch):
    monkeypatch.setattr(embedded, "ebook_meta_read",
        lambda p: {"title": "Microsoft Word - final2.doc", "author(s)": "Unknown"})
    assert embedded.resolve("x.pdf", "pdf") is None

def test_embedded_maps_fields(monkeypatch):
    monkeypatch.setattr(embedded, "ebook_meta_read", lambda p: {
        "title": "Router Table", "author(s)": "Bill Hylton", "identifiers": "isbn:9781462923229"})
    i = embedded.resolve("x.epub", "epub")
    assert (i.title, i.authors, i.isbn, i.confidence) == ("Router Table", ["Bill Hylton"], "9781462923229", 0.85)

@pytest.mark.tools
def test_pdftext_on_generated_pdf(tmp_path):  # build a 3-page PDF with PyMuPDF, read text back
    ...
```

The `tools` test builds a PDF with `fitz.open(); page.insert_text(...)` and asserts `page_count == 3` and that page text contains the inserted string. (PyMuPDF has no external binary, so this test can also run without the mark if PyMuPDF is installed locally; mark it `tools` only if CI lacks it.)

- [ ] **Step 2: Run** `pytest tests/test_tools.py tests/identify/test_embedded.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the modules above.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: tool wrapper, pdf text, embedded metadata resolver`

---

### Task 8: ISBN lookup resolver with cache

**Files:** Modify `src/shelfsort/identify/isbn.py`; Test `tests/identify/test_isbn_lookup.py`

**Interfaces:**
- Consumes: `run_tool`, `pdftext.page_text/page_count`, `find_isbns`, the `lookup_cache` table
- Produces:
  - `lookup_isbn(conn, isbn: str) -> Identity | None`: cache key `f"isbn:{isbn}"`. Cache hit → return it (including a cached negative `null`). Miss → `run_tool(["fetch-ebook-metadata","--isbn",isbn,"--opf"], timeout=90)`, parse the OPF XML (dc:title, dc:creator, dc:publisher, dc:date → year), store it, and return with `method="isbn"`, `confidence=0.95`. On `ToolTimeout`/network-type `ToolError`, raise `LookupRetry` (don't cache). Empty result → cache a negative and return None.
  - `class LookupRetry(Exception)`
  - `resolve(conn, path: str, ext: str, known_isbn: str | None) -> Identity | None`: use `known_isbn` if given; else, for pdf, text from pages `range(0,10)` and the last 5 pages; for epub/mobi/azw3, `ebook-convert` is not used. Use `ebook_meta_read`'s identifiers only (already handled in Task 7), so non-PDF without a known ISBN → None. Look up the first ISBN found.

- [ ] **Step 1: Write failing tests** (monkeypatch `run_tool` with fixture OPF text):

```python
OPF = """<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>A Beginner's Guide to House Plants</dc:title><dc:creator>Ryusuke Sakaino</dc:creator>
<dc:publisher>Tuttle</dc:publisher><dc:date>2022-03-01T00:00:00+00:00</dc:date></metadata></package>"""

def test_lookup_parses_and_caches(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: calls.append(a) or OPF)
    i = lookup_isbn(conn, "9781462923229"); lookup_isbn(conn, "9781462923229")
    assert (i.title, i.authors, i.year, i.confidence) == ("A Beginner's Guide to House Plants", ["Ryusuke Sakaino"], 2022, 0.95)
    assert len(calls) == 1

def test_lookup_timeout_raises_retry_and_not_cached(conn, monkeypatch):
    def boom(a, timeout): raise ToolTimeout("t")
    monkeypatch.setattr(isbn, "run_tool", boom)
    with pytest.raises(LookupRetry): lookup_isbn(conn, "9781462923229")
    assert conn.execute("select count(*) from lookup_cache").fetchone()[0] == 0
```

- [ ] **Step 2: Run** `pytest tests/identify/test_isbn_lookup.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Space out real `fetch-ebook-metadata` calls with a 1-second minimum interval (module-level last-call timestamp).
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: ISBN lookup via calibre with cache`

---

### Task 9: Magazine cover resolver (OCR)

**Files:** Create `src/shelfsort/identify/magazine.py`; Test `tests/identify/test_magazine.py`

**Interfaces:**
- Consumes: `render_page_png`, `Settings.magazines`
- Produces:
  - `@dataclass OcrWord(text:str, height:int, top:int)`
  - `ocr_words(png: bytes) -> list[OcrWord]` (pytesseract `image_to_data`, conf > 40)
  - `match_magazine(text: str, titles: list[str]) -> tuple[str, float] | None`: rapidfuzz `partial_ratio` on normalized strings; best match ≥ 85 → `(title, score/100)`.
  - `parse_issue_date(text: str) -> str | None`: `"YYYY-MM"` from patterns `April 2002`, `Apr 2002`, `April/May 2002` (first month), `2002-04`, `04-2002`, `April_2002`; seasons map Spring=03, Summer=06, Fall/Autumn=09, Winter=12.
  - `parse_issue_number(text: str) -> int | None`: `#95`, `No. 95`, `Issue 95`, `Number 95`.
  - `cover_story(words: list[OcrWord], masthead: str) -> str | None`: group words into lines by `top` (±10 px); drop lines fuzzy-matching the masthead (≥ 80) or containing a date/price/issue pattern; return the line with the greatest median word height, title-cased, if ≥ 3 characters.
  - `resolve(path: str, filename: str, titles: list[str]) -> Identity | None`: try `match_magazine` on the filename first (cheap), then OCR page 1. Date and issue come from cover text, then the filename. `method="cover-ocr"`. Confidence = match score × (1.0 if a date was found else 0.7).

- [ ] **Step 1: Write failing tests** (pure functions; OCR is mocked):

```python
def test_parse_issue_date():
    assert parse_issue_date("April_2002_Outdoor_Morris_Chair") == "2002-04"
    assert parse_issue_date("WINTER 2015 $6.99") == "2015-12"
    assert parse_issue_date("April/May 2019") == "2019-04"

def test_parse_issue_number():
    assert parse_issue_number("Issue #245") == 245 and parse_issue_number("no digits") is None

def test_match_magazine_fuzzy():
    assert match_magazine("AMERICAN W00DWORKER build it better", ["American Woodworker", "Wood"])[0] == "American Woodworker"

def test_cover_story_picks_biggest_non_masthead():
    words = [OcrWord("AMERICAN",120,10), OcrWord("WOODWORKER",120,10),
             OcrWord("Build",60,300), OcrWord("a",60,300), OcrWord("Morris",60,300), OcrWord("Chair",60,300),
             OcrWord("April",20,900), OcrWord("2002",20,900)]
    assert cover_story(words, "American Woodworker") == "Build A Morris Chair"

def test_resolve_with_mocked_ocr(monkeypatch):
    monkeypatch.setattr(magazine, "render_page_png", lambda p, page, dpi=200: b"")
    monkeypatch.setattr(magazine, "ocr_words", lambda png: [OcrWord("WOOD",150,5), OcrWord("Router",70,200),
        OcrWord("Jigs",70,200), OcrWord("Issue",15,900), OcrWord("#95",15,900), OcrWord("April",15,900), OcrWord("2002",15,900)])
    i = magazine.resolve("x.pdf", "011167038.pdf", ["Wood", "ShopNotes"])
    assert (i.magazine, i.issue_date, i.issue_number, i.cover_story) == ("Wood", "2002-04", 95, "Router Jigs")
```

- [ ] **Step 2: Run** `pytest tests/identify/test_magazine.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: magazine cover OCR resolver`

---

### Task 10: Ollama resolver and identification chain

**Files:** Create `src/shelfsort/identify/ollama.py`, `src/shelfsort/identify/__init__.py`; Test `tests/identify/test_chain.py`, `tests/identify/test_ollama.py`

**Interfaces:**
- Consumes: all resolvers above, `Settings`
- Produces:
  - `ollama.resolve(png: bytes, url: str, model: str, timeout: float = 120) -> Identity | None`: POST `{url}/api/generate` with `{"model", "prompt", "images":[b64], "format":"json", "stream":false}`. Prompt: return JSON `{"title","issue_date"(YYYY-MM),"issue_number","cover_story"}` for this magazine cover. Validate it: `issue_date` must match `^\d{4}-(0[1-9]|1[0-2])$` (otherwise None), `issue_number` must be an int (otherwise None). `method="ollama"`, `confidence=0.75`. On a connection error, raise `OllamaUnavailable`.
  - `identify_file(conn, rec: FileRecord, abspath: str, s: Settings) -> IdentifyResult` with `@dataclass IdentifyResult(best: Identity | None, all: list[Identity], status: FileStatus, note: str = "")`. Order: annas → embedded → isbn (pass the ISBN from earlier results) → magazine (pdf/cbz/images only) → ollama (only if `s.ollama_enabled` and the magazine resolver returned something below threshold or nothing, and ext is pdf/image). Stop at the first confidence ≥ `s.confidence`. Every result is stored in `identities`. Status: `identified` if best ≥ threshold; `retry` if `LookupRetry` was raised and nothing passed the threshold; `unreadable` if a `ToolError` came from pdftext; otherwise `review`. `OllamaUnavailable` → skip it and note `"ollama unreachable"`.

- [ ] **Step 1: Write failing tests** (patch each resolver):

```python
def test_chain_stops_at_first_confident(conn, rec, settings, patch_resolvers):
    calls = patch_resolvers(annas=Identity(title="T", confidence=0.95), embedded=AssertionError)
    r = identify_file(conn, rec, "/x.pdf", settings)
    assert r.status == FileStatus.identified and calls == ["annas"]

def test_chain_ollama_only_when_enabled(conn, rec, settings, patch_resolvers):
    calls = patch_resolvers(annas=None, embedded=None, isbn=None,
                            magazine=Identity(magazine="Wood", confidence=0.5), ollama=Identity(magazine="Wood", confidence=0.75))
    assert identify_file(conn, rec, "/x.pdf", settings).status == FileStatus.review and "ollama" not in calls
    settings.ollama_enabled = True; settings.confidence = 0.7
    assert identify_file(conn, rec, "/x.pdf", settings).best.method == "ollama"

def test_chain_unreadable_on_corrupt_pdf(conn, rec, settings, patch_resolvers):
    patch_resolvers(annas=None, embedded=None, isbn=ToolError("bad pdf"))
    assert identify_file(conn, rec, "/x.pdf", settings).status == FileStatus.unreadable

def test_ollama_rejects_bad_date(httpx_mock):  # pytest-httpx
    httpx_mock.add_response(json={"response": '{"title":"Wood","issue_date":"spring","issue_number":"x","cover_story":"Jigs"}'})
    i = ollama.resolve(b"png", "http://o:11434", "llava")
    assert i.issue_date is None and i.issue_number is None and i.magazine == "Wood"
```

`patch_resolvers` (conftest) monkeypatches each resolver to return the given value, raise it if it's an exception, or fail if it's `AssertionError`. It returns the list of called names.

- [ ] **Step 2: Run** `pytest tests/identify -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: resolver chain and optional ollama resolver`

---

### Task 11: Subject and kind classification

**Files:** Create `src/shelfsort/classify.py`; Test `tests/test_classify.py`

**Interfaces:**
- Produces:
  - `classify_subject(relpath: str, ident: Identity | None, filename: str, s: Settings) -> str | None`: top folder lowercased → `s.subject_map` lookup (keys are compared case-insensitively). If the top folder isn't mapped and isn't vague, use the folder name as-is (title-cased). Vague folder or a file at the root → keyword rules over `normalize_title(title + " " + filename)`. The first subject whose any keyword appears wins. None → review.
  - `classify_kind(ident: Identity | None, ext: str, pages: int | None, s: Settings) -> Kind | None`: (spec §5.4) isbn or (method in {annas, embedded, isbn} and authors) → BOOK; `ident.magazine` → MAGAZINE; ext in image types, or (ext == "pdf" and pages is not None and pages ≤ `s.plan_page_limit`) → PLAN; else None.

- [ ] **Step 1: Write failing tests**

```python
def test_subject_from_folder_map_and_fallback(settings):
    settings.subject_map = {"ww": "Woodworking"}
    assert classify_subject("WW/a.pdf", None, "a.pdf", settings) == "Woodworking"
    assert classify_subject("gardening/a.pdf", None, "a.pdf", settings) == "Gardening"

def test_subject_vague_uses_keywords(settings):
    settings.keyword_rules = {"Woodworking": ["router", "workbench", "morris chair"]}
    assert classify_subject("Downloads/x.pdf", None, "Garage-Workbench-Plans.pdf", settings) == "Woodworking"
    assert classify_subject("misc/x.pdf", None, "x.pdf", settings) is None

def test_kind_rules(settings):
    assert classify_kind(Identity(isbn="9781462923229"), "pdf", 300, settings) == Kind.BOOK
    assert classify_kind(Identity(magazine="Wood"), "pdf", 90, settings) == Kind.MAGAZINE
    assert classify_kind(None, "pdf", 12, settings) == Kind.PLAN
    assert classify_kind(None, "jpg", None, settings) == Kind.PLAN
    assert classify_kind(None, "pdf", 41, settings) is None
```

- [ ] **Step 2: Run** `pytest tests/test_classify.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: subject and kind classification`

---

### Task 12: Variant, near-copy, and possible-match groups

**Files:** Create `src/shelfsort/groups.py`; Test `tests/test_groups.py`

**Interfaces:**
- Consumes: `matching_key`, `normalize_title`, the best identity per file (`identities` row with max confidence), the subject (passed in)
- Produces: `build_groups(conn, items: list[GroupInput], fuzzy_threshold: int) -> None` with `@dataclass GroupInput(file_id:int, ext:str, sha256:str, subject:str|None, ident:Identity|None, filename:str)`. When `ident` is None or has no title and no magazine, the key uses `Identity(title=<filename stem with _ and - as spaces>)`, so unidentified plans can still group. Writes `groups(type in {"variant","near","possible"})` and one `review_items` row per group (reason = the group type). Rules (spec §5.5): same key with >1 distinct ext → variant (one group per key covering all members); same key, same ext, distinct sha → near (if a key has both, members of a repeated ext form a near group *and* the key forms a variant group). Possible: within the same subject, different keys, `token_set_ratio(norm titles) ≥ threshold` → pair groups (connected components via union-find, so A~B~C is one group). Files that are non-keepers of an exact group are excluded by the caller.

- [ ] **Step 1: Write failing tests**

```python
def gi(fid, ext, sha, title, subj="Woodworking"):
    return GroupInput(fid, ext, sha, subj, Identity(title=title), f"{title}.{ext}")

def test_unidentified_files_group_by_filename(conn):
    build_groups(conn, [GroupInput(1,"pdf","a","Woodworking",None,"Morris_Chair.pdf"),
                        GroupInput(2,"pdf","b","Woodworking",None,"Morris-Chair.pdf")], 90)
    assert [r["type"] for r in conn.execute("select type from groups")] == ["near"]

def test_variant_and_near(conn):
    build_groups(conn, [gi(1,"epub","a","Router Table"), gi(2,"lit","b","router table"),
                        gi(3,"pdf","c","Morris Chair"), gi(4,"pdf","d","Morris Chair")], 90)
    types = sorted(r["type"] for r in conn.execute("select type from groups"))
    assert types == ["near", "variant"]

def test_possible_match_same_subject_only(conn):
    build_groups(conn, [gi(1,"pdf","a","Weekend Woodworking Projects"),
                        gi(2,"pdf","b","American Woodworker Weekend Woodworking Projects"),
                        gi(3,"pdf","c","Weekend Woodworking Projects", subj="Gardening")], 90)
    members = [r["file_id"] for r in conn.execute(
        "select file_id from group_members join groups g on g.id=group_id where g.type='possible' order by file_id")]
    assert members == [1, 2]
```

(The third file shares a key with file 1 but is in another subject. Same-key groups are not subject-limited, since a duplicate is a duplicate. File 3 therefore lands in a `near` group with file 1, which this test doesn't assert on.)

- [ ] **Step 2: Run** `pytest tests/test_groups.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Fuzzy pass: bucket by first normalized token to avoid O(n²) on thousands of files, but also compare across buckets for titles sharing any 2 tokens. A simple inverted index on tokens is fine.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: variant, near-copy and possible-match grouping`

---

### Task 13: Plan builder

**Files:** Create `src/shelfsort/plan.py`; Test `tests/test_plan.py`

**Interfaces:**
- Consumes: `sanitize_component`, `Kind`, `PlanEntry`
- Produces:
  - `target_relpath(kind: Kind, subject: str, ident: Identity | None, filename: str, ext: str) -> str` (spec §5.6):
    - BOOK → `{Subject}/Books/{Author or "Unknown Author"}/{Title}{" (" + year + ")" if year}.{ext}`
    - MAGAZINE → `{Subject}/Magazines/{Magazine}/{Magazine} - {YYYY-MM or "undated"}{" (#n)" if n}.{ext}`
    - PLAN → `{Subject}/Plans & Articles/{clean title}.{ext}`, where clean title = ident.title or the filename stem with `_`/`-` turned into spaces, collapsed and title-cased.
    - Every component goes through `sanitize_component`.
  - `build_plan(conn, decisions: list[PlanInput]) -> list[PlanEntry]` with `@dataclass PlanInput(file_id, source, relpath, ext, kind, subject, ident, quarantine: bool)`. Quarantine entries → `action="quarantine"`, target `_quarantine/{relpath}`. Others → `"move"` (or `"rename"` if only the basename changes). Collisions are checked **per source, case-insensitively**, against (a) existing files on disk not being moved away by this plan and (b) earlier entries in the same plan. A collision → append ` (2)`, ` (3)`… before the extension and insert `review_items(reason="collision")`. Replaces existing `plan_entries` for those file ids.

- [ ] **Step 1: Write failing tests**

```python
def test_target_paths():
    assert target_relpath(Kind.MAGAZINE, "Woodworking", Identity(magazine="American Woodworker", issue_date="2002-04", issue_number=95), "x.pdf", "pdf") \
        == "Woodworking/Magazines/American Woodworker/American Woodworker - 2002-04 (#95).pdf"
    assert target_relpath(Kind.PLAN, "Woodworking", None, "arch-top-bookends<.pdf", "pdf") \
        == "Woodworking/Plans & Articles/Arch Top Bookends.pdf"
    assert target_relpath(Kind.BOOK, "Gardening", Identity(title="House Plants", authors=["R. Sakaino"], year=2022), "x.pdf", "pdf") \
        == "Gardening/Books/R. Sakaino/House Plants (2022).pdf"

def test_collision_within_plan_and_case_insensitive(conn, tmp_source):   # Review Focus 2 & 3
    tmp_source.write("Woodworking/Plans & Articles/morris chair.pdf")      # existing, not moving
    entries = build_plan(conn, [pi(1, "Morris Chair"), pi(2, "Morris_Chair")])
    assert [e.target_relpath.rsplit("/",1)[1] for e in entries] == ["Morris Chair (2).pdf", "Morris Chair (3).pdf"]
    assert conn.execute("select count(*) from review_items where reason='collision'").fetchone()[0] == 2
```

`pi(file_id, title)` builds a PLAN-kind `PlanInput` in subject Woodworking. `tmp_source` creates files under a temp source registered as "remote".

- [ ] **Step 2: Run** `pytest tests/test_plan.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: plan builder with collision handling`

---

### Task 14: Metadata writer

**Files:** Create `src/shelfsort/writemeta.py`; Test `tests/test_writemeta.py`

**Interfaces:**
- Consumes: `run_tool`, `Identity`
- Produces:
  - `opf_xml(ident: Identity) -> str`: OPF 2.0 with dc:title, dc:creator*, dc:publisher, dc:date, dc:identifier (opf:scheme="ISBN"), dc:description, and `calibre:series` / `calibre:series_index` meta for magazines.
  - `magazine_display_title(ident) -> str`: `"{Magazine} - {Month YYYY}"` (e.g. `American Woodworker - April 2002`), or just the magazine name if undated.
  - `ebook_meta_args(path, ident) -> list[str]`: `ebook-meta` args: `-t`, `-a` (authors joined with `&`), `--publisher`, `--date`, `--isbn`, `--comments` (= cover_story for magazines), `--series`, `--index`.
  - `backup_original(source_root: str, relpath: str) -> str`: copies to `{source_root}/_quarantine/_originals/{relpath}` (keeps an existing backup and never overwrites it) and returns the backup path.
  - `write_metadata(source_root, relpath, ident, ext, sidecar_all: bool) -> list[str]` returns paths written. Embedded for `epub pdf mobi azw azw3` via `ebook-meta`; `cbz` via a rewritten `ComicInfo.xml` inside the zip. It calls `backup_original` first for embedded writes. Sidecars `metadata.opf` (+ `cover.jpg` = page 1 render for pdf/cbz/images) go **next to the file** as `{stem}.opf` / `{stem}.jpg` (several files share a folder, so a bare `metadata.opf` would collide). They're written when `sidecar_all` or the format isn't writable.

- [ ] **Step 1: Write failing tests**

```python
def test_magazine_opf_has_series_and_description():
    x = opf_xml(Identity(magazine="Wood", issue_date="2002-04", issue_number=95, cover_story="Router Jigs"))
    assert "<dc:title>Wood - April 2002</dc:title>" in x and 'name="calibre:series" content="Wood"' in x
    assert 'content="95"' in x and "<dc:description>Router Jigs</dc:description>" in x

def test_backup_original_never_overwrites(tmp_path):
    (tmp_path/"a.pdf").write_bytes(b"v1"); backup_original(str(tmp_path), "a.pdf")
    (tmp_path/"a.pdf").write_bytes(b"v2"); p = backup_original(str(tmp_path), "a.pdf")
    assert open(p,"rb").read() == b"v1"

def test_lit_gets_sidecar_only(tmp_path, monkeypatch):
    calls = []; monkeypatch.setattr(writemeta, "run_tool", lambda a, timeout=60: calls.append(a) or "")
    (tmp_path/"x.lit").write_bytes(b"l")
    out = write_metadata(str(tmp_path), "x.lit", Identity(title="Router Table"), "lit", sidecar_all=False)
    assert calls == [] and out == [str(tmp_path/"x.opf")]
```

- [ ] **Step 2: Run** `pytest tests/test_writemeta.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: metadata writing with originals backup and sidecars`

---

### Task 15: Apply, journal, undo, recovery, mount checks

**Files:** Create `src/shelfsort/mounts.py`, `src/shelfsort/apply.py`; Test `tests/test_apply.py`, `tests/test_mounts.py`

**Interfaces:**
- Consumes: `plan_entries`, `write_metadata`, `backup_original`
- Produces:
  - `mounts.check_source(root: str) -> SourceStatus` with `@dataclass SourceStatus(mounted: bool, writable: bool, detail: str)`. Mounted = root exists, is a dir, and is non-empty or contains `.shelfsort` (create it on the first successful check). Writable = it can create and delete `.shelfsort/write-test`.
  - `apply.create_batch(conn, scope: dict) -> int`: scope keys are any of `source, subject, kind` and select planned entries.
  - `apply.run_batch(conn, batch_id, sources: dict[str,str], settings) -> BatchResult` with `@dataclass BatchResult(done:int, skipped:int, state:str)  # done|interrupted`. Per entry: `check_source` → if not mounted, mark the batch `interrupted` and stop. If the source file is missing or its size/mtime differ from the DB → skip, set the file to `review` with note `"source changed"`. Otherwise journal `pending` → `os.makedirs` + `os.rename` (same share; never `shutil.move` across sources) → if the final path exists, suffix ` (n)` → journal `done` → write metadata for non-quarantine moves (journal op `meta` with the backup path) → file status `organized`/`quarantined`.
  - `apply.undo_batch(conn, batch_id, sources) -> int`: reverse `done` journal entries in reverse `seq`: restore originals from backups for `meta`, rename `dst → src` for moves, and remove sidecars written. Mark entries `undone` and restore file statuses to `planned`.
  - `apply.recover(conn, sources) -> int`: for `pending` entries, `dst` exists and `src` doesn't → `done`; `src` exists → `failed`. Called at startup.
  - `apply.empty_quarantine(conn, source_root: str, confirm: str) -> int`: requires `confirm == "EMPTY"` (else `ValueError`); deletes `_quarantine/` contents (including `_originals`); marks undo unavailable for affected batches (`batches.state="final"`).

- [ ] **Step 1: Write failing tests**

```python
def test_apply_then_undo_restores_tree(fixture_source, conn, settings):
    before = snapshot(fixture_source.root)               # {relpath: sha256}
    bid = plan_and_batch(conn, fixture_source)           # helper: scan→plan→create_batch(all)
    assert run_batch(conn, bid, fixture_source.sources, settings).state == "done"
    assert snapshot(fixture_source.root) != before
    undo_batch(conn, bid, fixture_source.sources)
    assert snapshot(fixture_source.root, ignore={".shelfsort", "_quarantine"}) == before

def test_mount_loss_interrupts(fixture_source, conn, settings, monkeypatch):
    bid = plan_and_batch(conn, fixture_source); n = {"i": 0}
    def flaky(root): n["i"] += 1; return SourceStatus(n["i"] < 3, True, "")
    monkeypatch.setattr(apply, "check_source", flaky)
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.state == "interrupted" and r.done == 2

def test_changed_source_is_skipped(fixture_source, conn, settings):          # Review Focus 4
    bid = plan_and_batch(conn, fixture_source)
    os.remove(fixture_source.path("Woodworking/All-weather-Morris-Chair3.pdf"))
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.skipped == 1 and r.state == "done"

def test_quarantine_target_exists_is_suffixed(fixture_source, conn, settings):  # Review Focus 5
    fixture_source.write("_quarantine/Woodworking/dup.pdf", b"old")
    fixture_source.write("Woodworking/dup.pdf", b"new")
    bid = batch_with_entry(conn, fixture_source, "Woodworking/dup.pdf", action="quarantine")
    run_batch(conn, bid, fixture_source.sources, settings)
    assert fixture_source.read("_quarantine/Woodworking/dup.pdf") == b"old"
    assert fixture_source.exists("_quarantine/Woodworking/dup (2).pdf")

def test_recover_pending(conn, fixture_source):
    fixture_source.write("new/a.pdf", b"x")                     # dst exists, src gone
    jid = insert_journal(conn, state="pending", src="old/a.pdf", dst="new/a.pdf")
    fixture_source.write("old/b.pdf", b"y")                     # src still there
    jid2 = insert_journal(conn, state="pending", src="old/b.pdf", dst="new/b.pdf")
    assert recover(conn, fixture_source.sources) == 2
    assert journal_state(conn, jid) == "done" and journal_state(conn, jid2) == "failed"
```

`batch_with_entry`, `insert_journal` and `journal_state` are small conftest helpers that insert and read rows directly.

```python
def test_empty_quarantine_requires_confirm(conn, fixture_source):
    with pytest.raises(ValueError): empty_quarantine(conn, fixture_source.root, "yes")
```

- [ ] **Step 2: Run** `pytest tests/test_apply.py tests/test_mounts.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Metadata writing is monkeypatched to a no-op in these tests unless `-m tools`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: journaled apply, undo, recovery, quarantine emptying`

---

### Task 16: Pipeline orchestration and fixture library end-to-end test

**Files:** Create `src/shelfsort/pipeline.py`, `tests/fixtures/library.py`; Test `tests/test_e2e.py`

**Interfaces:**
- Consumes: every stage above
- Produces:
  - `run_scan_pipeline(conn, s: Settings, progress: Callable[[str,int,int],None]) -> PipelineSummary`. Steps, in order: scan → exact dupes → identify every non-ignored, non-duplicate file with status in {new, retry, review-with-no-user-decision} → classify (a file that identify left in `review` with no identity, but which classifies as `Kind.PLAN` with a non-None subject, becomes `identified` with `Identity(title=<clean filename title>, method="filename", confidence=0.8)`. Only files still lacking a kind or subject get a `review_items` row: `unidentified` if there's no identity, otherwise `subject`/`kind`) → groups → plan (files in unresolved review items are excluded from the plan; exact-dup non-keepers are planned as quarantine) → summary counts by status, subject, kind, and review reason. Progress is reported as `(stage, done, total)`.
  - `tests/fixtures/library.build(root) -> FixtureSource`: creates in `root/Woodworking/`: two byte-identical PDFs with different names; `Router Table.epub` + `Router Table.lit` (variant); two different `Morris Chair.pdf` in different subfolders (near-copy); a 3-page PDF whose page 1 says `WOOD` / `Router Jigs` / `Issue #95 April 2002`; a 2-page PDF named `arch-top-bookends<.pdf` (on Windows, a test without `<` is skipped via a platform check); a corrupt `broken.pdf`; `Shop Jigs -- Bill Hylton -- 2005 -- Taunton -- 1aa655cf2e3d698418bca2d8523a2531 -- Anna's Archive.epub` (identified as a Book from the filename, no lookup); `notes.txt`. The `Router Table` epub/lit have no embedded metadata.

- [ ] **Step 1: Write the failing e2e test** (resolvers that hit the network are patched; OCR runs only under `-m tools`, otherwise `ocr_words` is patched to return the known words):

```python
def test_full_dry_run_then_apply_then_undo(tmp_path, conn, settings):
    lib = library.build(tmp_path); settings.sources = lib.sources; settings.magazines = ["Wood"]
    before = snapshot(lib.root)
    summary = run_scan_pipeline(conn, settings, lambda *a: None)
    assert snapshot(lib.root) == before                       # dry run touches nothing
    assert summary.review_reasons == {"variant": 1, "near": 1, "unidentified": 2}  # Router Table .epub/.lit: no metadata, not PDF → no kind
    assert summary.status_counts["unreadable"] == 1 and summary.status_counts["ignored"] == 1
    bid = create_batch(conn, {"source": "remote"})
    run_batch(conn, bid, lib.sources, settings)
    assert lib.exists("Woodworking/Magazines/Wood/Wood - 2002-04 (#95).pdf")
    assert lib.exists("Woodworking/Plans & Articles/Arch Top Bookends.pdf")
    undo_batch(conn, bid, lib.sources)
    assert snapshot(lib.root, ignore={".shelfsort", "_quarantine"}) == before
```


- [ ] **Step 2: Run** `pytest tests/test_e2e.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `run_scan_pipeline` and the fixture builder.
- [ ] **Step 4: Run** `pytest -m "not tools" -v`. Expected: the whole suite PASSES.
- [ ] **Step 5: Commit** `feat: pipeline orchestration and end-to-end fixture test`

---

### Task 17: Background worker, dashboard, settings UI

**Files:** Create `src/shelfsort/jobs.py`, `src/shelfsort/web/app.py`, `web/routes_dashboard.py`, `web/routes_settings.py`, `web/templates/{base,dashboard,settings}.html`, `web/static/htmx.min.js` (vendored htmx 1.9.12); Test `tests/web/test_dashboard.py`, `tests/web/test_settings_ui.py`

**Interfaces:**
- Consumes: `run_scan_pipeline`, `check_source`, `apply.recover`, `settings.load/save`
- Produces:
  - `jobs.Worker` with `start(name: str, fn: Callable[[ProgressFn], Any]) -> bool` (False if a job is already running; one job at a time) and `status() -> JobStatus(name, stage, done, total, error, running)`. Runs in a thread with its own DB connection.
  - `web.app.create_app(db_path: str) -> FastAPI`. On startup it runs `apply.recover`. Routes: `GET /` dashboard; `POST /scan` (starts the pipeline job); `GET /job` (htmx progress fragment, polled every 2 s while running); `GET/POST /settings`. Settings form fields map to `Settings`. Lists (magazines, vague folders) are one item per line; `subject_map` and `keyword_rules` are `key = value[, value]` lines.
  - Dashboard shows a per-source `check_source` result, the last `PipelineSummary` (stored in `settings` table key `last_summary`), and warnings (unreachable Ollama, unmounted source).

- [ ] **Step 1: Write failing tests** with FastAPI `TestClient`:

```python
def test_dashboard_shows_unmounted_source(client_factory, tmp_path):
    client = client_factory(sources={"remote": str(tmp_path / "missing")})
    r = client.get("/"); assert r.status_code == 200 and "not mounted" in r.text

def test_scan_rejects_second_job(client, slow_job):
    assert client.post("/scan").status_code == 202
    assert client.post("/scan").status_code == 409

def test_settings_roundtrip(client):
    client.post("/settings", data={"magazines": "Wood\nShopNotes", "subject_map": "ww = Woodworking",
                                   "confidence": "0.8", "plan_page_limit": "40"})
    assert "ShopNotes" in client.get("/settings").text
```

- [ ] **Step 2: Run** `pytest tests/web -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: background worker, dashboard and settings UI`

---

### Task 18: Review, plan, apply/history UI

**Files:** Create `web/routes_review.py`, `web/routes_plan.py`, `web/routes_history.py`, `web/routes_files.py`, templates `review.html`, `review_group.html`, `review_single.html`, `plan.html`, `history.html`; Test `tests/web/test_review.py`, `tests/web/test_plan_apply.py`

**Interfaces:**
- Consumes: `review_items`, `groups`, `identities`, `build_plan`, `create_batch`, `run_batch`, `undo_batch`, `empty_quarantine`, `render_page_png`
- Produces routes:
  - `GET /review?tab=variant|near|possible|unidentified|magazine|subject` lists unresolved items 50 per page. `GET /review/{id}` returns a card fragment.
  - `POST /review/{id}/group` with `decisions[file_id]=keep|quarantine`. It requires at least one `keep` (400 otherwise); marks resolved; re-plans the affected files.
  - `POST /review/{id}/single` with editable identity fields + subject + kind. Saves the identity with `method="manual", confidence=1.0`; marks resolved; re-plans.
  - `POST /review/bulk` with `tab` + `action=accept_suggestions|keep_format` (+ `format=epub`). For `keep_format`, groups lacking that format are left unresolved.
  - `GET /files/{id}/cover.png`: page 1 render at 100 dpi, cached under `/config/thumbs/{sha256}.png`.
  - `GET /plan?source=&subject=&kind=` shows the table. `POST /apply` with a scope creates and runs a batch as a job.
  - `GET /history`; `POST /history/{batch_id}/undo`; `POST /quarantine/empty` (a form requiring the typed text `EMPTY`).
  - Keyboard shortcuts in `review.html` (plain JS, ~30 lines): `j/k` next/prev card, `a` accept, `q` quarantine the focused member, `s` save.

- [ ] **Step 1: Write failing tests**

```python
def test_group_decision_requires_a_keeper(client, variant_group):
    r = client.post(f"/review/{variant_group.id}/group",
                    data={f"decisions[{f}]": "quarantine" for f in variant_group.file_ids})
    assert r.status_code == 400

def test_single_edit_resolves_and_replans(client, conn, unidentified_item):
    client.post(f"/review/{unidentified_item.id}/single", data={"magazine": "Wood", "issue_date": "2002-04",
                "issue_number": "95", "cover_story": "Router Jigs", "subject": "Woodworking", "kind": "Magazines"})
    tgt = conn.execute("select target_relpath from plan_entries where file_id=?", (unidentified_item.file_id,)).fetchone()[0]
    assert tgt.endswith("Wood - 2002-04 (#95).pdf")

def test_bulk_keep_format(client, conn, two_variant_groups_one_without_epub):
    client.post("/review/bulk", data={"tab": "variant", "action": "keep_format", "format": "epub"})
    assert conn.execute("select count(*) from review_items where reason='variant' and resolved=0").fetchone()[0] == 1

def test_empty_quarantine_needs_typed_confirm(client):
    assert client.post("/quarantine/empty", data={"source": "remote", "confirm": "yes"}).status_code == 400
```

- [ ] **Step 2: Run** `pytest tests/web -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: review, plan, apply and history UI`

---

### Task 19: Container image and Unraid template

**Files:** Create `Dockerfile`, `docker-compose.yml`, `unraid/shelfsort.xml`, `README.md`

**Interfaces:**
- Consumes: `create_app`
- Produces:
  - Image: `python:3.12-slim-bookworm` + apt `tesseract-ocr tesseract-ocr-eng xz-utils libgl1 libegl1 libopengl0 libxcb-cursor0 libfontconfig1` + Calibre installed via its official Linux installer into `/opt/calibre` (`--isolated`), with `/opt/calibre` on PATH. Entrypoint: `uvicorn shelfsort.web.app:app --host 0.0.0.0 --port ${PORT:-8585}`. `USER 99:100`. `VOLUME /config`.
  - `docker-compose.yml` for local dev, mounting `./dev/sources/remote` and `./dev/config`.
  - The Unraid template defines the `8585` port; `/config` → `/mnt/user/appdata/shelfsort`; `/sources/remote` → `/mnt/remotes/<share>` (description tells the user to mount with Unassigned Devices); `/sources/local` → BookOrbit's library share; PUID/PGID 99/100.
  - The README covers the install steps, the first-run checklist (dry run → review → apply one small subject batch → check in BookOrbit → resolve open item §11.1 → set the sidecar toggle), and how to empty quarantine.

- [ ] **Step 1: Build** `docker compose build`. Expected: success.
- [ ] **Step 2: Run tool tests in the image** `docker compose run --rm app pytest -m tools -v`. Expected: PASS (ebook-meta, tesseract, real OCR on the fixture cover).
- [ ] **Step 3: Smoke test** `docker compose up -d`, then `curl -s localhost:8585/ | grep -q shelfsort`. Expected: exit 0. Run a scan from the UI against `dev/sources` and confirm the dashboard counts match the fixture.
- [ ] **Step 4: Validate the template** XML parses: `python -c "import xml.etree.ElementTree as E; E.parse('unraid/shelfsort.xml')"`.
- [ ] **Step 5: Commit** `build: docker image, compose and unraid template`

---

## Spec coverage map

| Spec | Task |
|---|---|
| §4.1–4.2 deployment, volumes, user | 19 |
| §4.3 state | 1 |
| §5.1 scan | 5, 2 (illegal names) |
| §5.2 exact dupes | 6 |
| §5.3 resolvers 1–6 | 4, 7, 3+8, 9, 10 |
| §5.4 subject/kind | 11 |
| §5.5 groups | 12 |
| §5.6 plan | 13 |
| §6 metadata writing, originals | 14, 15 (undo restores) |
| §7 UI 1–5 | 17, 18 |
| §8 errors | 7 (timeouts), 8 (retry), 10 (unreadable, ollama), 13 (collisions), 15 (mount, journal, permissions) |
| §9 testing | 16 (fixture e2e), 15 (recovery), 19 (dry run guidance in README) |
| §11.1 BookOrbit check | 19 README + `sidecar_all` toggle (1, 14) |
| §11.2 reuse | isbnlib (3), rapidfuzz (9, 12), calibre (7, 8, 14) |
| §11.3 image | 19 |
