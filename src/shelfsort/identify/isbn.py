"""ISBN extraction and validation."""

import dataclasses
import json
import re
import time
import xml.etree.ElementTree as ET

import isbnlib

from shelfsort import pdftext
from shelfsort.models import Identity
from shelfsort.tools import ToolError, ToolTimeout, run_tool

# A run of digit groups (last char of a group may be X) joined by single
# hyphens or spaces, bounded by non-digits on both sides.
_RUN_RE = re.compile(r"(?<!\d)\d[\dXx]*(?:[- ][\dXx]+)*(?!\d)")
_GROUP_SEP_RE = re.compile(r"[- ]")


def _canon(s: str) -> str:
    return re.sub(r"[- ]", "", s).upper()


def valid_isbn(s: str) -> bool:
    """True if s (hyphens/spaces ignored) is a checksum-valid ISBN-10 or ISBN-13."""
    c = _canon(s)
    return bool(isbnlib.is_isbn10(c) or isbnlib.is_isbn13(c))


def _isbns_in_run(run: str) -> list[str]:
    """Greedily pick valid ISBNs from contiguous groups of one run."""
    groups = _GROUP_SEP_RE.split(run)
    found: list[str] = []
    i = 0
    while i < len(groups):
        digits = ""
        best10 = best13 = None
        for j in range(i, len(groups)):
            digits += groups[j].upper()
            if len(digits) > 13:
                break
            if len(digits) == 13 and valid_isbn(digits):
                best13 = (j, digits)
            elif len(digits) == 10 and valid_isbn(digits):
                best10 = (j, digits)
        hit = best13 or best10
        if hit:
            found.append(hit[1])
            i = hit[0] + 1
        else:
            i += 1
    return found


def find_isbns(text: str) -> list[str]:
    """Canonical, deduped, checksum-valid ISBNs in order of appearance.

    A 10-digit ISBN whose ISBN-13 equivalent also appears is dropped.
    """
    candidates: list[str] = []
    for m in _RUN_RE.finditer(text):
        candidates.extend(_isbns_in_run(m.group()))
    thirteens = {c for c in candidates if len(c) == 13}
    out: list[str] = []
    for c in candidates:
        if len(c) == 10 and isbnlib.to_isbn13(c) in thirteens:
            continue
        if c not in out:
            out.append(c)
    return out


class LookupRetry(Exception):
    """Transient lookup failure (timeout/network); nothing was cached."""


_MIN_INTERVAL = 1.0
_last_call = float("-inf")
_sleep = time.sleep

_NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
_YEAR_RE = re.compile(r"\d{4}")


def _throttle() -> None:
    """Block so real calls are at least _MIN_INTERVAL apart."""
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        _sleep(wait)
    _last_call = time.monotonic()


def _parse_opf(text: str, isbn: str) -> Identity | None:
    if not text.strip():
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    def texts(tag: str) -> list[str]:
        return [e.text.strip() for e in root.iterfind(f".//dc:{tag}", _NS) if e.text and e.text.strip()]

    titles = texts("title")
    if not titles:
        return None
    publishers = texts("publisher")
    year = None
    dates = texts("date")
    if dates and (m := _YEAR_RE.match(dates[0])):
        year = int(m.group())
    return Identity(
        title=titles[0], authors=texts("creator"), year=year,
        publisher=publishers[0] if publishers else None,
        isbn=isbn, method="isbn", confidence=0.95,
    )


def lookup_isbn(conn, isbn: str) -> Identity | None:
    """Metadata for an ISBN via calibre, cached (negatives too) in lookup_cache."""
    key = f"isbn:{isbn}"
    row = conn.execute("select json from lookup_cache where key = ?", (key,)).fetchone()
    if row is not None:
        data = json.loads(row[0])
        return None if data is None else Identity(**data)

    _throttle()
    try:
        out = run_tool(["fetch-ebook-metadata", "--isbn", isbn, "--opf", "--timeout", "60"],
                       timeout=90)
    except ToolTimeout as e:
        raise LookupRetry(str(e)) from e
    except ToolError as e:
        if "no results found" not in str(e).lower():
            raise LookupRetry(str(e)) from e
        out = ""
    ident = _parse_opf(out, isbn)
    conn.execute(
        "insert or replace into lookup_cache(key, json, fetched) values (?, ?, ?)",
        (key, json.dumps(None if ident is None else dataclasses.asdict(ident)),
         str(int(time.time()))),
    )
    conn.commit()
    return ident


def resolve(conn, path: str, ext: str, known_isbn: str | None) -> Identity | None:
    """Identify by ISBN: a known one, else the first found in a PDF's key pages."""
    if known_isbn:
        return lookup_isbn(conn, known_isbn)
    if ext != "pdf":
        return None
    n = pdftext.page_count(path)
    pages = sorted(set(range(min(10, n))) | set(range(max(0, n - 5), n)))
    found = find_isbns(pdftext.page_text(path, pages))
    return lookup_isbn(conn, found[0]) if found else None
