import io

import pytest

from shelfsort.identify import magazine
from shelfsort.identify.magazine import (
    OcrWord,
    cover_story,
    match_magazine,
    ocr_words,
    parse_issue_date,
    parse_issue_number,
)
from shelfsort.tools import ToolError


def test_parse_issue_date():
    assert parse_issue_date("April_2002_Outdoor_Morris_Chair") == "2002-04"
    assert parse_issue_date("WINTER 2015 $6.99") == "2015-12"
    assert parse_issue_date("April/May 2019") == "2019-04"


def test_parse_issue_date_other_forms():
    assert parse_issue_date("Apr 2002") == "2002-04"
    assert parse_issue_date("issue 2002-04") == "2002-04"
    assert parse_issue_date("mag 04-2002") == "2002-04"
    assert parse_issue_date("Spring 1999") == "1999-03"
    assert parse_issue_date("Summer 1999") == "1999-06"
    assert parse_issue_date("Fall 1999") == "1999-09"
    assert parse_issue_date("Autumn 1999") == "1999-09"
    assert parse_issue_date("no date here 011167038") is None


def test_parse_issue_number():
    assert parse_issue_number("Issue #245") == 245 and parse_issue_number("no digits") is None


def test_parse_issue_number_forms():
    assert parse_issue_number("#95") == 95
    assert parse_issue_number("No. 95") == 95
    assert parse_issue_number("Number 95") == 95


def test_match_magazine_fuzzy():
    assert match_magazine("AMERICAN W00DWORKER build it better", ["American Woodworker", "Wood"])[0] == "American Woodworker"


def test_match_magazine_score_and_none():
    title, score = match_magazine("Wood magazine", ["Wood"])
    assert title == "Wood" and score == 1.0
    assert match_magazine("completely unrelated text", ["American Woodworker"]) is None


def test_match_magazine_keeps_years_intact():
    assert match_magazine("2002", ["Zooo"]) is None


def test_cover_story_picks_biggest_non_masthead():
    words = [OcrWord("AMERICAN",120,10), OcrWord("WOODWORKER",120,10),
             OcrWord("Build",60,300), OcrWord("a",60,300), OcrWord("Morris",60,300), OcrWord("Chair",60,300),
             OcrWord("April",20,900), OcrWord("2002",20,900)]
    assert cover_story(words, "American Woodworker") == "Build A Morris Chair"


def test_cover_story_tolerates_x_height_top_offset():
    words = [OcrWord("Build", 60, 300), OcrWord("a", 60, 315), OcrWord("Morris", 60, 300),
             OcrWord("Chair", 60, 300), OcrWord("WOOD", 20, 5)]
    assert cover_story(words, "Wood") == "Build A Morris Chair"


def test_cover_story_masthead_filter_is_whole_line():
    words = [OcrWord("Build", 60, 300), OcrWord("a", 60, 300), OcrWord("Wood", 60, 300),
             OcrWord("Chair", 60, 300), OcrWord("W00D", 90, 5)]
    assert cover_story(words, "Wood") == "Build A Wood Chair"
    assert cover_story([OcrWord("WOOD", 90, 5), OcrWord("Jigs", 30, 200)], "Wood") == "Jigs"


def test_cover_story_none_when_too_short():
    assert cover_story([OcrWord("Ab", 80, 100)], "Wood") is None


def test_resolve_with_mocked_ocr(monkeypatch):
    monkeypatch.setattr(magazine, "render_page_png", lambda p, page, dpi=200: b"")
    monkeypatch.setattr(magazine, "ocr_words", lambda png: [OcrWord("WOOD",150,5), OcrWord("Router",70,200),
        OcrWord("Jigs",70,200), OcrWord("Issue",15,900), OcrWord("#95",15,900), OcrWord("April",15,900), OcrWord("2002",15,900)])
    i = magazine.resolve("x.pdf", "011167038.pdf", ["Wood", "ShopNotes"])
    assert (i.magazine, i.issue_date, i.issue_number, i.cover_story) == ("Wood", "2002-04", 95, "Router Jigs")
    assert i.method == "cover-ocr" and i.confidence == 1.0


def test_resolve_filename_match_survives_ocr_failure(monkeypatch):
    def boom(p, page, dpi=200):
        raise ToolError("nope")

    monkeypatch.setattr(magazine, "render_page_png", boom)
    i = magazine.resolve("x.pdf", "American_Woodworker_April_2002_Issue_95.pdf", ["American Woodworker"])
    assert (i.magazine, i.issue_date, i.issue_number, i.cover_story) == ("American Woodworker", "2002-04", 95, None)
    assert i.confidence == 1.0


def test_resolve_no_date_lowers_confidence(monkeypatch):
    monkeypatch.setattr(magazine, "render_page_png", lambda p, page, dpi=200: b"")
    monkeypatch.setattr(magazine, "ocr_words", lambda png: [OcrWord("ShopNotes", 100, 5)])
    i = magazine.resolve("x.pdf", "scan.pdf", ["ShopNotes"])
    assert i.magazine == "ShopNotes" and i.issue_date is None and i.confidence == 0.7


def test_resolve_none_when_no_title_matches(monkeypatch):
    monkeypatch.setattr(magazine, "render_page_png", lambda p, page, dpi=200: b"")
    monkeypatch.setattr(magazine, "ocr_words", lambda png: [OcrWord("Cooking", 100, 5)])
    assert magazine.resolve("x.pdf", "scan.pdf", ["Wood"]) is None


def test_resolve_never_raises_on_ocr_error(monkeypatch):
    monkeypatch.setattr(magazine, "render_page_png", lambda p, page, dpi=200: b"")

    def boom(png):
        raise RuntimeError("tesseract missing")

    monkeypatch.setattr(magazine, "ocr_words", boom)
    assert magazine.resolve("x.pdf", "scan.pdf", ["Wood"]) is None


def test_resolve_non_pdf_skips_ocr(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("must not render")

    monkeypatch.setattr(magazine, "render_page_png", fail)
    i = magazine.resolve("x.cbz", "Wood_2002-04.cbz", ["Wood"])
    assert i.magazine == "Wood" and i.issue_date == "2002-04" and i.cover_story is None


@pytest.mark.tools
def test_ocr_words_real_tesseract():
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (800, 200), "white")
    ImageDraw.Draw(img).text((20, 40), "WOODWORKER", fill="black", font=ImageFont.load_default(size=80))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    words = ocr_words(buf.getvalue())
    assert any("WOOD" in w.text.upper() for w in words)
