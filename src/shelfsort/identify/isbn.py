"""ISBN extraction and validation."""

import re

import isbnlib

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
