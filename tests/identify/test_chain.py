import json

from shelfsort import pdftext
from shelfsort.identify import annas, identify_file, isbn, ollama
from shelfsort.models import FileStatus, Identity
from shelfsort.tools import ToolError


def test_chain_stops_at_first_confident(conn, rec, settings, patch_resolvers):
    calls = patch_resolvers(annas=Identity(title="T", confidence=0.95), embedded=AssertionError)
    r = identify_file(conn, rec, "/x.pdf", settings)
    assert r.status == FileStatus.IDENTIFIED and calls == ["annas"]


def test_chain_ollama_only_when_enabled(conn, rec, settings, patch_resolvers):
    calls = patch_resolvers(
        annas=None, embedded=None, isbn=None,
        magazine=Identity(magazine="Wood", confidence=0.5),
        ollama=Identity(magazine="Wood", method="ollama", confidence=0.75))
    assert identify_file(conn, rec, "/x.pdf", settings).status == FileStatus.REVIEW
    assert "ollama" not in calls
    settings.ollama_enabled = True
    settings.confidence = 0.7
    assert identify_file(conn, rec, "/x.pdf", settings).best.method == "ollama"


def test_chain_unreadable_on_corrupt_pdf(conn, rec, settings, patch_resolvers):
    calls = patch_resolvers(annas=None, embedded=None, isbn=ToolError("bad pdf"))
    assert identify_file(conn, rec, "/x.pdf", settings).status == FileStatus.UNREADABLE
    assert calls[-1] == "magazine"  # chain continues past the error


def test_chain_retry_and_unreadable_precedence(conn, rec, settings, patch_resolvers):
    patch_resolvers(isbn=isbn.LookupRetry("net"))
    assert identify_file(conn, rec, "/x.pdf", settings).status == FileStatus.RETRY
    patch_resolvers(isbn=ToolError("bad"))
    assert identify_file(conn, rec, "/x.pdf", settings).status == FileStatus.UNREADABLE


def test_chain_retry_overridden_by_confident_later_result(conn, rec, settings, patch_resolvers):
    patch_resolvers(isbn=isbn.LookupRetry("net"),
                    magazine=Identity(magazine="W", method="magazine", confidence=0.9))
    r = identify_file(conn, rec, "/x.pdf", settings)
    assert r.status == FileStatus.IDENTIFIED and r.best.method == "magazine"


def test_chain_passes_basename_to_annas(conn, rec, settings, patch_resolvers, monkeypatch):
    seen = {}
    patch_resolvers()
    monkeypatch.setattr(annas, "parse_annas", lambda fn: seen.update(fn=fn))
    identify_file(conn, rec, "/a/b/x.pdf", settings)
    assert seen == {"fn": "x.pdf"}


def test_chain_known_isbn_from_earlier_result(conn, rec, settings, patch_resolvers, monkeypatch):
    seen = {}
    patch_resolvers(annas=Identity(isbn="9780306406157", confidence=0.5))
    monkeypatch.setattr(isbn, "resolve", lambda c, p, e, k: seen.update(isbn=k))
    identify_file(conn, rec, "/x.pdf", settings)
    assert seen == {"isbn": "9780306406157"}


def test_chain_no_known_isbn_passes_none(conn, rec, settings, patch_resolvers, monkeypatch):
    seen = {}
    patch_resolvers(annas=Identity(title="A", confidence=0.5))
    monkeypatch.setattr(isbn, "resolve", lambda c, p, e, k: seen.update(isbn=k))
    identify_file(conn, rec, "/x.pdf", settings)
    assert seen == {"isbn": None}


def test_chain_magazine_skipped_for_epub(conn, rec, settings, patch_resolvers):
    rec.ext = "epub"
    calls = patch_resolvers()
    identify_file(conn, rec, "/x.epub", settings)
    assert calls == ["annas", "embedded", "isbn"]


def test_chain_best_ties_go_to_earlier(conn, rec, settings, patch_resolvers):
    patch_resolvers(annas=Identity(title="A", method="annas", confidence=0.5),
                    embedded=Identity(title="E", method="embedded", confidence=0.5),
                    isbn=Identity(title="I", method="isbn", confidence=0.6))
    assert identify_file(conn, rec, "/x.pdf", settings).best.method == "isbn"
    patch_resolvers(annas=Identity(title="A", method="annas", confidence=0.5),
                    embedded=Identity(title="E", method="embedded", confidence=0.5))
    assert identify_file(conn, rec, "/x.pdf", settings).best.method == "annas"


def test_chain_ollama_unreachable_noted(conn, rec, settings, patch_resolvers):
    settings.ollama_enabled = True
    patch_resolvers(ollama=ollama.OllamaUnavailable("down"))
    r = identify_file(conn, rec, "/x.pdf", settings)
    assert r.status == FileStatus.REVIEW and r.note == "ollama unreachable"


def test_chain_ollama_skipped_when_render_fails(conn, rec, settings, patch_resolvers, monkeypatch):
    settings.ollama_enabled = True
    calls = patch_resolvers(ollama=Identity(magazine="W", confidence=0.75))

    def boom(*a, **k):
        raise ToolError("render")

    monkeypatch.setattr(pdftext, "render_page_png", boom)
    assert identify_file(conn, rec, "/x.pdf", settings).status == FileStatus.REVIEW
    assert "ollama" not in calls


def test_chain_ollama_reads_image_bytes(conn, rec, settings, patch_resolvers, monkeypatch, tmp_path):
    settings.ollama_enabled = True
    rec.ext = "jpg"
    img = tmp_path / "c.jpg"
    img.write_bytes(b"JPEGDATA")
    seen = {}
    patch_resolvers()
    monkeypatch.setattr(ollama, "resolve", lambda png, url, model: seen.update(png=png))
    identify_file(conn, rec, str(img), settings)
    assert seen == {"png": b"JPEGDATA"}


def test_chain_not_ollama_for_cbz(conn, rec, settings, patch_resolvers):
    settings.ollama_enabled = True
    rec.ext = "cbz"
    calls = patch_resolvers()
    identify_file(conn, rec, "/x.cbz", settings)
    assert "ollama" not in calls and "magazine" in calls


def test_chain_stores_rows_and_keeps_manual(conn, rec, settings, patch_resolvers):
    conn.execute("insert into identities(file_id, method, json, confidence) values (?, 'manual', '{}', 1.0)", (rec.id,))
    conn.execute("insert into identities(file_id, method, json, confidence) values (?, 'annas', '{}', 0.1)", (rec.id,))
    conn.commit()
    patch_resolvers(annas=Identity(title="A", method="annas", confidence=0.5),
                    embedded=Identity(title="E", method="embedded", confidence=0.6))
    identify_file(conn, rec, "/x.pdf", settings)
    identify_file(conn, rec, "/x.pdf", settings)  # re-identify: no duplicates
    rows = conn.execute("select method, json, confidence from identities where file_id=?", (rec.id,)).fetchall()
    assert sorted(r["method"] for r in rows) == ["annas", "embedded", "manual"]
    annas_row = next(r for r in rows if r["method"] == "annas")
    assert json.loads(annas_row["json"])["title"] == "A" and annas_row["confidence"] == 0.5
    assert conn.execute("select status from files where id=?", (rec.id,)).fetchone()[0] == "new"
