from __future__ import annotations

import re
import unicodedata

from shelfsort.models import Identity

_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
_EXT = re.compile(r"^(.*)(\.[A-Za-z0-9]{1,5})$", re.DOTALL)
_ANNA_TAIL = re.compile(r"\s*--\s*anna['’]?s\s+archive.*$", re.DOTALL)
_APOSTROPHES = re.compile(r"['’‘`]")
_NOISE = frozenset(
    {"the", "a", "an", "edition", "ed", "vol", "volume", "annas", "archive"}
)


def sanitize_component(s: str, max_len: int = 200) -> str:
    """Make `s` safe as a single path component (keeps non-Latin text)."""
    s = unicodedata.normalize("NFC", _ILLEGAL.sub("", s))
    s = " ".join(s.split())
    s = s.rstrip(". ")
    ext = ""
    m = _EXT.match(s)
    if m:
        s, ext = m.group(1), m.group(2)
    s = s.rstrip(". ").lstrip()
    if not s:
        s = "untitled"
    s = s[: max(1, max_len - len(ext))].rstrip(". ") or "untitled"
    return s + ext


def _fold_latin(s: str) -> str:
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", s):
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    return "".join(out)


def normalize_title(s: str) -> str:
    s = _ANNA_TAIL.sub("", s.lower())
    s = _fold_latin(s)
    s = _APOSTROPHES.sub("", s)
    s = "".join(
        " " if unicodedata.category(ch)[0] in "PSZC" else ch for ch in s
    )
    return " ".join(w for w in s.split() if w not in _NOISE)


def matching_key(ident: Identity) -> str | None:
    if ident.magazine:
        if not ident.issue_date:
            return None
        return f"mag|{normalize_title(ident.magazine)}|{ident.issue_date}"
    if not ident.title:
        return None
    author = normalize_title(ident.authors[0]) if ident.authors else ""
    return f"book|{normalize_title(ident.title)}|{author}"
