# Ebook Organizer — Design Spec

- **Date:** 2026-10-12
- **Status:** Draft, awaiting user review
- **Working name:** `shelfsort` (placeholder; rename freely)

## 1. Purpose

Clean up a collection of thousands of ebooks, magazines, and woodworking plans
(and other subjects) before consolidating it into BookOrbit on Unraid.

The tool must:

1. Remove exact duplicate files automatically (to a quarantine, not deletion).
2. Surface format variants, near-copies, and fuzzy title matches for human decision.
3. Identify each file and create/correct its metadata — books via ISBN, magazines
   via their cover.
4. Sort files into a **subject-first** folder tree:
   `<Subject>/{Books, Magazines, Plans & Articles}/...`
5. Run as an Unraid Docker app with a web GUI, reaching both local and remote (NAS) files.
6. Work without AI by default. AI is an optional, local-only fallback for magazines.

### Success criteria

- Every scanned file ends in exactly one state: organized, quarantined, ignored
  (non-ebook), or in the review queue.
- No file is ever deleted or overwritten by the tool. Every applied change is undoable
  until the user explicitly empties quarantine.
- Books with a findable ISBN, and Anna's Archive–named files, are identified with no
  user involvement.
- Magazines whose title is in the user's magazine list get title, issue date, issue number,
  and cover story filled in, or land in review with the cover shown.
- A full rescan of an unchanged library completes in minutes (hash cache).

## 2. Context and current state

- **BookOrbit** (self-hosted library, Unraid template) holds part of the collection on Unraid.
  It finds book metadata reasonably well but **finds nothing for magazines**.
- **The majority of files** are loose (not a Calibre library) on a remote server, in
  folders that already roughly indicate subject (e.g. `Woodworking/`).
- The user will move the cleaned result to Unraid **after** cleanup.
- Observed file naming (sample of one folder):
  - Anna's Archive pattern: `Title -- Author -- Year -- Publisher -- ISBN -- MD5 -- Anna's Archive.ext`
  - Opaque numeric names: `011167038.pdf`, `459331368<.pdf`
  - Descriptive names: `April_2002_Outdoor_Morris_Chair.pdf`, `All-weather-Morris-Chair3.pdf`
  - Characters Windows/SMB doesn't allow, notably a trailing `<`
  - Same title in multiple formats (`.epub` + `.lit`)
  - Single-project plans/articles and image plans (`.jpg`), not just books and full magazines

## 3. Decisions made

| Topic | Decision |
|---|---|
| Overall approach | Custom web app; reuse Calibre CLI + Tesseract rather than reimplementing |
| Where changes happen | In place, within each source share. User migrates to Unraid afterward. |
| Folder layout | Subject first: `<Subject>/Books`, `<Subject>/Magazines`, `<Subject>/Plans & Articles` |
| Subject source | Current folder, mapped through an editable table; keyword rules for vague folders |
| Exact duplicates | Automatic: keep one, quarantine the rest |
| Format variants / near-copies / fuzzy matches | Always to the review queue |
| Magazine metadata | Title, issue date, issue number, main cover story (in description) |
| AI | Off by default. Optional local Ollama vision model, magazines only. No cloud AI. |
| "Remove" | Move to `_quarantine/` on the same share. Deletion only via explicit "Empty quarantine". |
| Applying | In user-chosen batches (e.g. per subject), each undoable |

## 4. Architecture

### 4.1 Deployment

- One Docker container, installed via an Unraid template; web UI on a configurable port
  (default `8585`).
- Image contents: Python 3 app, Calibre CLI (`ebook-meta`, `fetch-ebook-metadata`),
  Tesseract OCR, PDF tooling (PyMuPDF).
- Optional separate Ollama container, referenced by URL in Settings.
- Runs as `99:100` (`nobody:users`), Unraid's convention.

### 4.2 Volumes

| Container path | Host | Mode |
|---|---|---|
| `/config` | `/mnt/user/appdata/shelfsort` | rw: SQLite DB, caches, logs |
| `/sources/remote` | Unassigned Devices mount of the NAS share (SMB/NFS) | rw |
| `/sources/local` | BookOrbit's library share | rw |
| `/sources/<name>` | Any additional source | rw |

Sources are configured in Settings; each must be a mounted folder.

### 4.3 State (SQLite in `/config`)

- `files`: path, source, size, mtime, sha256, format, page count, status
- `identities`: per file — fields found (title, author, year, publisher, ISBN, magazine
  title, issue date, issue #, cover story), **method** that produced them, **confidence**
- `groups`: exact-duplicate, variant, near-copy, and possible-match groups and their members
- `review_items`: reason, linked file/group, user decision
- `plan_entries`: file → target path, target name, metadata to write, action
  (move / quarantine / rename / write-metadata)
- `batches` + `journal`: applied operations, written *before* and confirmed *after*
  each operation, for undo and crash recovery
- `lookup_cache`: ISBN / API responses
- `settings`: subject map, magazine list, keyword rules, thresholds, Ollama config

## 5. Pipeline

Each stage is a separate module with a narrow interface, so it can be tested in isolation.
Stages up to Plan are read-only against the sources.

### 5.1 Scan

- Walk every source. Record path, size, mtime, SHA-256 (skip rehash when size+mtime are unchanged).
- Handled types: `pdf, epub, mobi, azw, azw3, lit, fb2, djvu, cbz, cbr, jpg, jpeg, png`.
  Other files are recorded as *ignored* and left alone.
- Skip `_quarantine/` and the tool's own output folders.
- Flag names containing characters Windows/SMB doesn't allow (`< > : " | ? *`, trailing dot/space)
  so they get renamed.

### 5.2 Exact duplicates

- Group by SHA-256 across **all** sources.
- Keeper rule, in order: (1) the copy in the local/BookOrbit source; (2) the copy with the
  more descriptive filename (not purely numeric); (3) the copy in a non-vague folder;
  (4) the shortest path.
- Non-keepers → planned for quarantine. No review needed.

### 5.3 Identify

Resolvers run in order; each returns fields + confidence. Stop at the first result
with confidence ≥ threshold (configurable, default 0.8). Results from all resolvers
that ran are stored for display in review.

1. **Anna's Archive filename parser.** Split on ` -- `; map fields; extract ISBN-10/13
   and validate the checksum; ignore the MD5 and site tag. Treat `Unknown` / `Desconocido`
   as an empty author.
2. **Embedded metadata.** EPUB OPF, PDF info/XMP, MOBI/AZW3 headers (via `ebook-meta`).
   Discard placeholder junk ("Microsoft Word - ...", "untitled", scanner names).
3. **ISBN extraction + lookup.** Extract text from the first 10 and last 5 pages
   (PyMuPDF; OCR if there's no text layer). Find checksum-valid ISBNs. Look up with
   `fetch-ebook-metadata --isbn` (Open Library, Google Books). Cache results.
4. **Magazine cover.** Render page 1 → Tesseract OCR → fuzzy-match against the
   **magazine title list** (also try the filename). On a match:
   - issue date from month/season + year patterns (cover text, then filename)
   - issue number from `#NNN`, `No. NNN`, `Issue NNN` patterns
   - cover story = largest-font OCR text block that isn't the masthead (use Tesseract's
     word bounding-box heights), cleaned
5. **Local AI (optional).** If enabled and the file is likely a magazine with low confidence,
   send the cover image to the Ollama vision model. Prompt it for JSON:
   `{title, issue_date, issue_number, cover_story}`. Validate the fields; the result
   still counts as "AI" in review if below threshold.
6. **Unresolved** → review queue (reason: *unidentified*).

### 5.4 Sort

- **Subject:** `subject_map[current top-level folder]` (case-insensitive, editable). If the
  folder is in the *vague* list (`Downloads`, `Misc`, `New folder`, ...), apply keyword rules
  to title + filename. No match → review (reason: *uncertain subject*).
- **Kind:**
  - ISBN or book-metadata match → **Books**
  - Magazine-list match → **Magazines**
  - No ISBN, no magazine match, and (image, or PDF with page count ≤ threshold, default 40)
    → **Plans & Articles**
  - Otherwise → review (reason: *uncertain kind*)

### 5.5 Variants, near-copies, possible matches

- **Matching key:** normalized title + first author (lowercase, ASCII-fold, strip
  punctuation, edition/volume noise words, site tags). Magazines: title + issue date.
- Same key, different format → **variant group** → review.
- Same key, same format, different hash → **near-copy group** → review.
- Fuzzy title similarity ≥ 90 (rapidfuzz token-set ratio) within the same subject,
  keys not identical → **possible-match group** → review.

### 5.6 Plan

Build target paths and names:

- Books: `<Subject>/Books/<Author>/<Title> (<Year>).<ext>`
- Magazines: `<Subject>/Magazines/<Magazine>/<Magazine> - <YYYY-MM>[ (#<issue>)].<ext>`
- Plans & Articles: `<Subject>/Plans & Articles/<Clean title>.<ext>`

Sanitize all components (characters Windows/SMB doesn't allow, length ≤ 200 per name).
A collision with an existing path adds ` (2)` and creates a review item; the tool never overwrites.

## 6. Writing metadata

- **Embedded**, where the format allows: EPUB / PDF / MOBI / AZW3 via `ebook-meta`;
  CBZ via `ComicInfo.xml`.
  - Magazines: title = `<Magazine> - <Month YYYY>`, series = magazine name,
    series index = issue number, publisher, pubdate, description = cover story.
- **Sidecar** `metadata.opf` + `cover.jpg` for formats without writable metadata
  (`.lit`, images, `djvu`) — and optionally for all files (see §11).
- **The filename** always carries the key fields, as a fallback.
- **Originals preserved:** before a file's first in-place metadata write, copy the
  original to `_quarantine/_originals/<relative path>`. Undo restores from there.

## 7. Web UI

Server-rendered (FastAPI + Jinja templates + htmx). Long jobs run in a background
worker; the UI polls for progress.

1. **Dashboard:** source status (mounted, writable), job progress, counts per
   status / subject / kind, warnings (e.g. Ollama unreachable).
2. **Review:** tabs per reason — Variants, Near-copies, Possible matches, Unidentified,
   Uncertain magazines, Uncertain subject/kind.
   - Group items: side-by-side cards (cover thumbnail, format, size, pages, metadata
     source, confidence); keep/quarantine per member.
   - Single items: large cover + editable fields with the guesses pre-filled.
   - Bulk actions (accept all suggestions in the tab; "keep format X in all variant groups").
   - Keyboard shortcuts (next/prev, keep, quarantine, accept).
3. **Plan:** filterable table of planned changes (old → new path, metadata diff);
   select a scope (source / subject / kind) to make a batch.
4. **Apply & History:** apply a batch with progress; list of past batches with
   **Undo**. **Empty quarantine** is separate and needs confirmation.
5. **Settings:** sources, subject map, vague-folder list, keyword rules, magazine title
   list, page-count threshold, confidence threshold, Ollama enable/URL/model, OPF-sidecar toggle.

## 8. Error handling

- **Mount loss:** check that the source is mounted (a sentinel file plus a mount check) before
  each batch and each operation. If it's gone, mark the batch *interrupted*, stop, and keep the
  journal; resume or undo later. A missing file is never treated as deleted.
- **Crash safety:** the journal entry is written before each operation and confirmed after.
  At startup, reconcile unconfirmed entries by checking whether the source or target exists.
- **Collisions:** never overwrite; suffix the name and add a review item.
- **Corrupt files:** each external call (Calibre, Tesseract, PyMuPDF, Ollama) runs with a
  timeout. On failure, mark the file *unreadable*, add it to review, and continue.
- **Network APIs:** rate-limit and cache. When a lookup fails, mark the file *retry later*,
  not failed.
- **Permissions:** at startup, write-test each source and `/config`; show any failures
  on the dashboard.

## 9. Testing

- **Unit tests:** Anna's Archive parser (fixtures taken from the real filenames in §2),
  ISBN extraction and checksum validation, name normalization and matching keys, fuzzy grouping,
  subject/kind decisions, path building and sanitizing, magazine date/issue patterns.
- **Fixture library integration test:** generate a small tree (exact duplicates, epub/lit variants,
  near-copies, a fake magazine cover PDF with known masthead text, a corrupt PDF,
  a `<` filename). Run scan → plan → apply → undo, then assert the tree is byte-identical
  to the start.
- **Journal recovery test:** kill partway through apply; restart; verify reconciliation.
- **Operational:** the first real use is a dry run against the remote share. Review the plan
  and counts before any apply. The first apply is one small subject batch, checked in BookOrbit.

## 10. Out of scope (YAGNI)

- Moving files from the remote server to Unraid (the user does this separately).
- Full magazine contents beyond the main cover story.
- Format conversion (e.g. `.lit` → `.epub`).
- Writing directly into BookOrbit's database or API.
- Cloud AI services.
- Multi-user access or authentication beyond what Unraid's network already provides.

## 11. Open items to verify during implementation

1. **How BookOrbit reads metadata.** Public info confirms that BookOrbit *writes* metadata
   into files and has a watched import folder, but not whether it reads `metadata.opf`
   sidecars. **Default: write embedded metadata plus an OPF sidecar for every file** (toggle
   in Settings). Verify with a test import of a few processed files before the first
   real batch, then turn sidecars off if they aren't needed.
2. **Reuse check.** Before writing a component, check for maintained libraries that
   cover it (e.g. `isbnlib` for ISBN validation, `rapidfuzz`, an existing Anna's Archive
   filename parser) and prefer them.
3. **Calibre image size.** The Calibre CLI is large. Pick a base image (official Calibre
   binaries on Debian-slim vs. an existing Calibre container image) during planning.
