"""Identify a magazine issue from its filename and first-page (cover) OCR."""

from __future__ import annotations

import io
import os
import re
import statistics
from dataclasses import dataclass

from rapidfuzz import fuzz

from shelfsort.models import Identity
from shelfsort.names import normalize_title
from shelfsort.pdftext import render_page_png

_MATCH_THRESHOLD = 85
_MASTHEAD_THRESHOLD = 80
_MIN_CONF = 40
_LINE_TOLERANCE = 10
_NO_DATE_FACTOR = 0.7

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_SEASONS = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}
_MONTH_WORD = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_YEAR = re.compile(
    rf"\b({_MONTH_WORD})\.?(?:\s*[/&-]\s*{_MONTH_WORD}\.?)?,?\s+((?:19|20)\d\d)\b", re.I
)
_SEASON_YEAR = re.compile(
    r"\b(spring|summer|fall|autumn|winter)\s+((?:19|20)\d\d)\b", re.I
)
_YEAR_MONTH = re.compile(r"(?<!\d)((?:19|20)\d\d)-(0[1-9]|1[0-2])(?!\d)")
_MONTH_YEAR_NUM = re.compile(r"(?<!\d)(0[1-9]|1[0-2])-((?:19|20)\d\d)(?!\d)")
_ISSUE_NO = re.compile(r"(?:#|\b(?:no\.?|issue|number)\s*#?)\s*(\d{1,4})\b", re.I)
_PRICE = re.compile(r"\$\d")
_OCR_ZERO = re.compile(r"\b(?=[A-Za-z0]*[A-Za-z])[A-Za-z0]*0[A-Za-z0]*\b")


@dataclass
class OcrWord:
    text: str
    height: int
    top: int


def ocr_words(png: bytes) -> list[OcrWord]:
    import pytesseract
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words: list[OcrWord] = []
    for text, conf, height, top in zip(
        data["text"], data["conf"], data["height"], data["top"]
    ):
        text = str(text).strip()
        if text and float(conf) > _MIN_CONF:
            words.append(OcrWord(text, int(height), int(top)))
    return words


def _prepare(text: str) -> str:
    """Normalize for matching, fixing OCR zeros inside otherwise-alphabetic tokens."""
    text = text.replace("_", " ")
    text = _OCR_ZERO.sub(lambda m: m.group(0).replace("0", "o"), text)
    return normalize_title(text)


def match_magazine(text: str, titles: list[str]) -> tuple[str, float] | None:
    haystack = _prepare(text)
    best: tuple[str, float] | None = None
    for title in titles:
        needle = normalize_title(title)
        if not needle or not haystack:
            continue
        score = fuzz.partial_ratio(needle, haystack)
        if score < _MATCH_THRESHOLD:
            continue
        if best is None or score > best[1] or (
            score == best[1] and len(title) > len(best[0])
        ):
            best = (title, score)
    if best is None:
        return None
    return best[0], best[1] / 100


def parse_issue_date(text: str) -> str | None:
    text = text.replace("_", " ")
    if m := _MONTH_YEAR.search(text):
        return f"{m.group(2)}-{_MONTHS[m.group(1)[:3].lower()]:02d}"
    if m := _SEASON_YEAR.search(text):
        return f"{m.group(2)}-{_SEASONS[m.group(1).lower()]:02d}"
    if m := _YEAR_MONTH.search(text):
        return f"{m.group(1)}-{m.group(2)}"
    if m := _MONTH_YEAR_NUM.search(text):
        return f"{m.group(2)}-{m.group(1)}"
    return None


def parse_issue_number(text: str) -> int | None:
    m = _ISSUE_NO.search(text.replace("_", " "))
    return int(m.group(1)) if m else None


def cover_story(words: list[OcrWord], masthead: str) -> str | None:
    lines: list[list[OcrWord]] = []
    start_top = 0
    for w in sorted(words, key=lambda w: w.top):
        if lines and w.top - start_top <= _LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
            start_top = w.top
    mast = normalize_title(masthead)
    best: tuple[float, str] | None = None
    for line in lines:
        text = " ".join(w.text for w in line)
        norm = normalize_title(text)
        if mast and norm and fuzz.partial_ratio(mast, norm) >= _MASTHEAD_THRESHOLD:
            continue
        if (
            parse_issue_date(text)
            or parse_issue_number(text) is not None
            or _PRICE.search(text)
        ):
            continue
        height = statistics.median(w.height for w in line)
        if best is None or height > best[0]:
            best = (height, text)
    if best is None:
        return None
    story = best[1].title()
    return story if len(story) >= 3 else None


def resolve(path: str, filename: str, titles: list[str]) -> Identity | None:
    stem = os.path.splitext(filename)[0]
    match = match_magazine(stem, titles)
    words: list[OcrWord] = []
    if os.path.splitext(filename)[1].lower() == ".pdf":
        try:
            words = ocr_words(render_page_png(path, 0))
        except Exception:
            words = []
    text = " ".join(w.text for w in words)
    if match is None:
        match = match_magazine(text, titles)
    if match is None:
        return None
    title, score = match
    issue_date = parse_issue_date(text) or parse_issue_date(stem)
    issue_number = parse_issue_number(text)
    if issue_number is None:
        issue_number = parse_issue_number(stem)
    return Identity(
        magazine=title,
        issue_date=issue_date,
        issue_number=issue_number,
        cover_story=cover_story(words, title) if words else None,
        method="cover-ocr",
        confidence=score * (1.0 if issue_date else _NO_DATE_FACTOR),
    )
