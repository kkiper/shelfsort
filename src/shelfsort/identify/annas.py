"""Parser for Anna's Archive style filenames."""

from __future__ import annotations

import re

from shelfsort.identify.isbn import valid_isbn
from shelfsort.models import Identity

_SEP = " -- "
_TAIL_RE = re.compile(r"Anna['\u2019]s Archive$")
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_YEAR_RE = re.compile(r"(?<!\d)(1[4-9]\d\d|20\d\d)(?!\d)")
_YEAR_SEG_RE = re.compile(r"[\d, ]+")
_ISBN_SEG_RE = re.compile(r"[\dXx -]+")
_MD5_RE = re.compile(r"[0-9a-fA-F]{32}")
_NO_AUTHOR = {"unknown", "desconocido", "anonymous"}


def _stem(filename: str) -> str | None:
    name = re.split(r"[\/]", filename)[-1].strip()
    if _TAIL_RE.search(name):
        return name
    name = _EXT_RE.sub("", name).rstrip()
    return name if _TAIL_RE.search(name) else None


def parse_annas(filename: str) -> Identity | None:
    stem = _stem(filename)
    if stem is None or _SEP not in stem:
        return None
    parts = [p.strip() for p in stem.split(_SEP)][:-1]  # drop "Anna's Archive"
    if not parts or not parts[0]:
        return None

    ident = Identity(title=parts[0].replace("_ ", ": "), method="annas", confidence=0.9)
    if len(parts) > 1 and parts[1] and parts[1].lower() not in _NO_AUTHOR:
        ident.authors = [parts[1]]

    for seg in parts[2:]:
        if not seg or _MD5_RE.fullmatch(seg):
            continue
        if _ISBN_SEG_RE.fullmatch(seg) and valid_isbn(seg):
            if ident.isbn is None:
                ident.isbn = re.sub(r"[- ]", "", seg).upper()
            continue
        if _YEAR_SEG_RE.fullmatch(seg):
            m = _YEAR_RE.search(seg)
            if m:
                if ident.year is None:
                    ident.year = int(m.group())
                continue
        if ident.publisher is None:
            ident.publisher = seg

    if ident.isbn:
        ident.confidence = 0.95
    return ident
