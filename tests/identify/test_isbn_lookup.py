import json

import pytest

from shelfsort.identify import isbn
from shelfsort.identify.isbn import LookupRetry, lookup_isbn, resolve
from shelfsort.tools import ToolError, ToolTimeout

OPF = """<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>A Beginner's Guide to House Plants</dc:title><dc:creator>Ryusuke Sakaino</dc:creator>
<dc:publisher>Tuttle</dc:publisher><dc:date>2022-03-01T00:00:00+00:00</dc:date></metadata></package>"""

ISBN13 = "9781462923229"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(isbn, "_sleep", lambda s: None)
    monkeypatch.setattr(isbn, "_last_call", float("-inf"))


def _rows(conn):
    return conn.execute("select count(*) from lookup_cache").fetchone()[0]


def test_lookup_parses_and_caches(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: calls.append(a) or OPF)
    i = lookup_isbn(conn, ISBN13)
    lookup_isbn(conn, ISBN13)
    assert (i.title, i.authors, i.year, i.confidence) == (
        "A Beginner's Guide to House Plants", ["Ryusuke Sakaino"], 2022, 0.95)
    assert (i.publisher, i.isbn, i.method) == ("Tuttle", ISBN13, "isbn")
    assert len(calls) == 1
    assert calls[0] == ["fetch-ebook-metadata", "--isbn", ISBN13, "--opf", "--timeout", "60"]


def test_lookup_multiple_creators(conn, monkeypatch):
    opf = OPF.replace("<dc:creator>Ryusuke Sakaino</dc:creator>",
                      "<dc:creator>A One</dc:creator><dc:creator>B Two</dc:creator>")
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: opf)
    assert lookup_isbn(conn, ISBN13).authors == ["A One", "B Two"]


def test_cache_stores_identity_json_and_timestamp(conn, monkeypatch):
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: OPF)
    lookup_isbn(conn, ISBN13)
    key, js, fetched = conn.execute("select key, json, fetched from lookup_cache").fetchone()
    assert key == f"isbn:{ISBN13}"
    assert json.loads(js)["title"] == "A Beginner's Guide to House Plants"
    assert float(fetched) > 1_000_000_000


def test_lookup_timeout_raises_retry_and_not_cached(conn, monkeypatch):
    def boom(a, timeout):
        raise ToolTimeout("t")
    monkeypatch.setattr(isbn, "run_tool", boom)
    with pytest.raises(LookupRetry):
        lookup_isbn(conn, ISBN13)
    assert _rows(conn) == 0


def test_other_tool_error_is_retry_not_cached(conn, monkeypatch):
    def boom(a, timeout):
        raise ToolError("fetch-ebook-metadata exited with 1: connection reset")
    monkeypatch.setattr(isbn, "run_tool", boom)
    with pytest.raises(LookupRetry):
        lookup_isbn(conn, ISBN13)
    assert _rows(conn) == 0


def test_no_results_caches_negative(conn, monkeypatch):
    calls = []

    def nores(a, timeout):
        calls.append(a)
        raise ToolError("fetch-ebook-metadata exited with 1: Found 0 results\n\nNo results found")
    monkeypatch.setattr(isbn, "run_tool", nores)
    assert lookup_isbn(conn, ISBN13) is None
    assert lookup_isbn(conn, ISBN13) is None
    assert len(calls) == 1
    assert conn.execute("select json from lookup_cache").fetchone()[0] == "null"


@pytest.mark.parametrize("out", ["", "  \n", "<package xmlns='http://www.idpf.org/2007/opf'><metadata/></package>"])
def test_empty_output_or_no_title_caches_negative(conn, monkeypatch, out):
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: out)
    assert lookup_isbn(conn, ISBN13) is None
    assert conn.execute("select json from lookup_cache").fetchone()[0] == "null"


def test_rate_limit_spacing_and_cache_hits_exempt(conn, monkeypatch):
    sleeps = []
    clock = [100.0]
    monkeypatch.setattr(isbn, "_sleep", sleeps.append)
    monkeypatch.setattr(isbn.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: OPF)
    lookup_isbn(conn, ISBN13)
    assert sleeps == []
    clock[0] = 100.4
    lookup_isbn(conn, "9780306406157")
    assert sleeps == [pytest.approx(0.6)]
    sleeps.clear()
    lookup_isbn(conn, ISBN13)  # cache hit: no sleep
    assert sleeps == []


def test_resolve_known_isbn(conn, monkeypatch):
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: OPF)
    assert resolve(conn, "x.epub", "epub", ISBN13).title.startswith("A Beginner")


def test_resolve_non_pdf_without_isbn_is_none(conn):
    assert resolve(conn, "x.epub", "epub", None) is None


def test_resolve_pdf_scans_first_ten_and_last_five(conn, monkeypatch):
    seen = {}
    monkeypatch.setattr(isbn.pdftext, "page_count", lambda p: 30)

    def fake_text(p, pages):
        seen["pages"] = list(pages)
        return f"ISBN {ISBN13}"
    monkeypatch.setattr(isbn.pdftext, "page_text", fake_text)
    monkeypatch.setattr(isbn, "run_tool", lambda a, timeout: OPF)
    assert resolve(conn, "x.pdf", "pdf", None).isbn == ISBN13
    assert seen["pages"] == list(range(10)) + list(range(25, 30))


def test_resolve_pdf_short_no_duplicate_pages(conn, monkeypatch):
    seen = {}
    monkeypatch.setattr(isbn.pdftext, "page_count", lambda p: 7)
    monkeypatch.setattr(isbn.pdftext, "page_text", lambda p, pages: seen.setdefault("p", list(pages)) and "none")
    assert resolve(conn, "x.pdf", "pdf", None) is None
    assert seen["p"] == list(range(7))


def test_resolve_pdf_no_isbn_found(conn, monkeypatch):
    monkeypatch.setattr(isbn.pdftext, "page_count", lambda p: 3)
    monkeypatch.setattr(isbn.pdftext, "page_text", lambda p, pages: "no isbn")
    assert resolve(conn, "x.pdf", "pdf", None) is None


def test_resolve_pdf_error_propagates(conn, monkeypatch):
    def bad(p):
        raise ToolError("corrupt")
    monkeypatch.setattr(isbn.pdftext, "page_count", bad)
    with pytest.raises(ToolError):
        resolve(conn, "x.pdf", "pdf", None)


def test_resolve_retry_propagates(conn, monkeypatch):
    def boom(a, timeout):
        raise ToolTimeout("t")
    monkeypatch.setattr(isbn, "run_tool", boom)
    with pytest.raises(LookupRetry):
        resolve(conn, "x.epub", "epub", ISBN13)


@pytest.mark.tools
def test_real_fetch_ebook_metadata(conn):
    # Network call; excluded from the default suite.
    i = lookup_isbn(conn, "9780306406157")
    assert i is None or i.isbn == "9780306406157"
