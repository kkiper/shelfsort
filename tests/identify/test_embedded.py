import pytest

from shelfsort.identify import embedded
from shelfsort.tools import ToolError


def _patch(monkeypatch, meta):
    monkeypatch.setattr(embedded, "ebook_meta_read", lambda p: meta)


def test_embedded_discards_junk(monkeypatch):
    _patch(monkeypatch, {"title": "Microsoft Word - final2.doc", "author(s)": "Unknown"})
    assert embedded.resolve("x.pdf", "pdf") is None


@pytest.mark.parametrize(
    "title",
    ["Untitled", "untitled document", "Unknown", "scan0001", "Document", "document12",
     "Adobe Acrobat", "MICROSOFT WORD - x"],
)
def test_embedded_junk_titles(monkeypatch, title):
    _patch(monkeypatch, {"title": title, "author(s)": "Bill Hylton"})
    assert embedded.resolve("x.pdf", "pdf") is None


def test_document_prefix_not_junk(monkeypatch):
    _patch(monkeypatch, {"title": "Document Design Handbook"})
    assert embedded.resolve("x.pdf", "pdf").title == "Document Design Handbook"


def test_title_equal_to_stem_is_junk(monkeypatch):
    _patch(monkeypatch, {"title": "MyBook", "author(s)": "A B"})
    assert embedded.resolve("/some/dir/mybook.pdf", "pdf") is None


def test_embedded_maps_fields(monkeypatch):
    _patch(monkeypatch, {
        "title": "Router Table", "author(s)": "Bill Hylton", "identifiers": "isbn:9781462923229"})
    i = embedded.resolve("x.epub", "epub")
    assert (i.title, i.authors, i.isbn, i.confidence) == ("Router Table", ["Bill Hylton"], "9781462923229", 0.85)
    assert i.method == "embedded"


def test_authors_split_and_sort_names(monkeypatch):
    _patch(monkeypatch, {
        "title": "T", "author(s)": "Ann One [One, Ann] & Bob Two [Two, Bob]",
        "published": "2012-05-01T00:00:00+00:00", "publisher": "Fox"})
    i = embedded.resolve("x.epub", "epub")
    assert i.authors == ["Ann One", "Bob Two"]
    assert i.year == 2012
    assert i.publisher == "Fox"


def test_title_only(monkeypatch):
    _patch(monkeypatch, {"title": "Some Title", "author(s)": "Unknown"})
    i = embedded.resolve("x.epub", "epub")
    assert i.authors == [] and i.confidence == 0.7


def test_isbn10_identifier_canonicalized_and_multi_ids(monkeypatch):
    _patch(monkeypatch, {"title": "T", "identifiers": "mobi-asin:B00X, isbn:1-56619-909-3"})
    assert embedded.resolve("x.epub", "epub").isbn == "1566199093"


def test_invalid_isbn_ignored(monkeypatch):
    _patch(monkeypatch, {"title": "T", "identifiers": "isbn:1234567890123"})
    assert embedded.resolve("x.epub", "epub").isbn is None


def test_no_title_returns_none(monkeypatch):
    _patch(monkeypatch, {"author(s)": "Bill Hylton"})
    assert embedded.resolve("x.epub", "epub") is None


def test_tool_error_returns_none(monkeypatch):
    def boom(p):
        raise ToolError("x")

    monkeypatch.setattr(embedded, "ebook_meta_read", boom)
    assert embedded.resolve("x.epub", "epub") is None
