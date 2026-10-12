"""Identify a file from its embedded metadata (via Calibre's ebook-meta)."""

from __future__ import annotations

import os
import re

from shelfsort.identify.isbn import find_isbns
from shelfsort.models import Identity
from shelfsort.tools import ToolError, ebook_meta_read

_JUNK_RE = re.compile(r"^(microsoft word - |untitled|unknown|scan|document\d*$|adobe )", re.I)
_SORT_NAME_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_YEAR_RE = re.compile(r"\b(\d{4})\b")
_ISBN_ID_RE = re.compile(r"isbn:\s*([0-9Xx\- ]+)", re.I)


def _authors(raw: str) -> list[str]:
    names = [_SORT_NAME_RE.sub("", a).strip() for a in raw.split(" & ")]
    return [n for n in names if n and n.lower() != "unknown"]


def _isbn(raw: str) -> str | None:
    for m in _ISBN_ID_RE.finditer(raw):
        found = find_isbns(m.group(1))
        if found:
            return found[0]
    return None


def resolve(path: str, ext: str) -> Identity | None:
    try:
        meta = ebook_meta_read(path)
    except ToolError:
        return None
    title = meta.get("title", "").strip()
    stem = os.path.splitext(os.path.basename(path))[0]
    if not title or _JUNK_RE.match(title) or title.lower() == stem.lower():
        return None
    authors = _authors(meta.get("author(s)", ""))
    year = None
    m = _YEAR_RE.search(meta.get("published", ""))
    if m:
        year = int(m.group(1))
    return Identity(
        title=title,
        authors=authors,
        year=year,
        publisher=meta.get("publisher") or None,
        isbn=_isbn(meta.get("identifiers", "")),
        method="embedded",
        confidence=0.85 if authors else 0.7,
    )
