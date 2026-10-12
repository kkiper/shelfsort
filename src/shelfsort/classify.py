from __future__ import annotations

from shelfsort.models import Identity, Kind
from shelfsort.names import normalize_title
from shelfsort.settings import Settings

_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png"})
_BOOK_METHODS = frozenset({"annas", "embedded", "isbn"})


def _top_folder(relpath: str) -> str | None:
    parts = [p for p in relpath.split("/") if p]
    return parts[0] if len(parts) > 1 else None


def _keyword_subject(
    ident: Identity | None, filename: str, s: Settings
) -> str | None:
    title = ident.title if ident and ident.title else ""
    hay = f" {normalize_title(f'{title} {filename}')} "
    for subject, keywords in s.keyword_rules.items():
        for kw in keywords:
            nk = normalize_title(kw)
            if nk and (f" {nk} " in hay or f" {nk}s " in hay):
                return subject
    return None


def classify_subject(
    relpath: str, ident: Identity | None, filename: str, s: Settings
) -> str | None:
    folder = _top_folder(relpath)
    if folder is not None:
        key = folder.lower()
        mapped = {k.lower(): v for k, v in s.subject_map.items()}
        if key in mapped:
            return mapped[key]
        if key not in {v.lower() for v in s.vague_folders}:
            return folder.title() if folder == folder.lower() else folder
    return _keyword_subject(ident, filename, s)


def classify_kind(
    ident: Identity | None, ext: str, pages: int | None, s: Settings
) -> Kind | None:
    if ident and (
        ident.isbn or (ident.method in _BOOK_METHODS and ident.authors)
    ):
        return Kind.BOOK
    if ident and ident.magazine:
        return Kind.MAGAZINE
    ext = ext.lower().lstrip(".")
    if ext in _IMAGE_EXTS or (
        ext == "pdf" and pages is not None and pages <= s.plan_page_limit
    ):
        return Kind.PLAN
    return None
