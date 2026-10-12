"""Identification chain: annas -> embedded -> isbn -> magazine -> ollama."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from shelfsort import pdftext
from shelfsort.identify import annas, embedded, isbn, magazine, ollama
from shelfsort.models import FileRecord, FileStatus, Identity
from shelfsort.settings import Settings
from shelfsort.tools import ToolError

_MAGAZINE_EXTS = {"pdf", "cbz", "jpg", "jpeg", "png"}
_OLLAMA_EXTS = {"pdf", "jpg", "jpeg", "png"}


@dataclass
class IdentifyResult:
    best: Identity | None
    all: list[Identity] = field(default_factory=list)
    status: FileStatus = FileStatus.REVIEW
    note: str = ""


def _cover_png(abspath: str, ext: str) -> bytes | None:
    try:
        if ext == "pdf":
            return pdftext.render_page_png(abspath, 0)
        with open(abspath, "rb") as f:
            return f.read()
    except (ToolError, OSError):
        return None


def identify_file(conn, rec: FileRecord, abspath: str, s: Settings) -> IdentifyResult:
    ext = rec.ext
    filename = os.path.basename(abspath)
    results: list[Identity] = []
    notes: list[str] = []
    unreadable = False
    retry = False

    def best_of() -> Identity | None:
        best = None
        for r in results:
            if best is None or r.confidence > best.confidence:
                best = r
        return best

    def done() -> bool:
        b = best_of()
        return b is not None and b.confidence >= s.confidence

    def known_isbn() -> str | None:
        return next((r.isbn for r in results if r.isbn), None)

    steps = [
        ("annas", lambda: annas.parse_annas(filename)),
        ("embedded", lambda: embedded.resolve(abspath, ext)),
        ("isbn", lambda: isbn.resolve(conn, abspath, ext, known_isbn())),
    ]
    if ext in _MAGAZINE_EXTS:
        steps.append(("magazine", lambda: magazine.resolve(abspath, filename, s.magazines)))

    mag_result: Identity | None = None
    for name, step in steps:
        if done():
            break
        try:
            r = step()
        except isbn.LookupRetry:
            retry = True
            continue
        except ToolError:
            unreadable = True
            continue
        if name == "magazine":
            mag_result = r
        if r is not None:
            results.append(r)

    if (not done() and s.ollama_enabled and ext in _OLLAMA_EXTS
            and (mag_result is None or mag_result.confidence < s.confidence)):
        png = _cover_png(abspath, ext)
        if png is not None:
            try:
                r = ollama.resolve(png, s.ollama_url, s.ollama_model)
                if r is not None:
                    results.append(r)
            except ollama.OllamaUnavailable:
                notes.append("ollama unreachable")

    best = best_of()
    if best is not None and best.confidence >= s.confidence:
        status = FileStatus.IDENTIFIED
    elif unreadable:
        status = FileStatus.UNREADABLE
    elif retry:
        status = FileStatus.RETRY
    else:
        status = FileStatus.REVIEW

    conn.execute("delete from identities where file_id=? and method != 'manual'", (rec.id,))
    for r in results:
        conn.execute(
            "insert into identities(file_id, method, json, confidence) values (?, ?, ?, ?)",
            (rec.id, r.method, json.dumps(asdict(r)), r.confidence))
    conn.commit()
    return IdentifyResult(best=best, all=results, status=status, note="; ".join(notes))
