import io
import os
import zipfile

import pymupdf
import pytest
from PIL import Image

from shelfsort import writemeta
from shelfsort.models import Identity
from shelfsort.tools import ToolError, run_tool
from shelfsort.writemeta import (
    backup_original,
    ebook_meta_args,
    magazine_display_title,
    opf_xml,
    write_metadata,
)


def _png(color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buf, "PNG")
    return buf.getvalue()


def _pdf(path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "hello")
    doc.save(path)
    doc.close()


def test_magazine_opf_has_series_and_description():
    x = opf_xml(Identity(magazine="Wood", issue_date="2002-04", issue_number=95, cover_story="Router Jigs"))
    assert "<dc:title>Wood - April 2002</dc:title>" in x and 'name="calibre:series" content="Wood"' in x
    assert 'content="95"' in x and "<dc:description>Router Jigs</dc:description>" in x
    assert "<dc:date>2002-04-01</dc:date>" in x
    assert 'version="2.0"' in x


def test_opf_book_fields_and_escaping():
    x = opf_xml(Identity(title="A & B <c>", authors=["X", "Y"], year=1999, publisher="P&Q", isbn="9780306406157"))
    assert "<dc:title>A &amp; B &lt;c&gt;</dc:title>" in x
    assert x.count("<dc:creator") == 2
    assert "<dc:publisher>P&amp;Q</dc:publisher>" in x
    assert "<dc:date>1999-01-01</dc:date>" in x
    assert 'opf:scheme="ISBN"' in x and "9780306406157" in x
    assert "calibre:series" not in x


def test_display_title():
    assert magazine_display_title(Identity(magazine="American Woodworker", issue_date="2002-04")) == "American Woodworker - April 2002"
    assert magazine_display_title(Identity(magazine="Wood")) == "Wood"


def test_ebook_meta_args_book_and_magazine():
    a = ebook_meta_args("/x/b.epub", Identity(title="T", authors=["A", "B"], year=2001, publisher="Pub", isbn="123"))
    assert a[:2] == ["ebook-meta", "/x/b.epub"]
    assert a[a.index("-t") + 1] == "T"
    assert a[a.index("-a") + 1] == "A & B"
    assert a[a.index("--date") + 1] == "2001-01-01"
    assert a[a.index("--isbn") + 1] == "123"
    assert "--series" not in a and "--comments" not in a
    m = ebook_meta_args("/x/m.pdf", Identity(magazine="Wood", issue_date="2002-04", issue_number=95, cover_story="Jigs"))
    assert m[m.index("-t") + 1] == "Wood - April 2002"
    assert m[m.index("--date") + 1] == "2002-04-01"
    assert m[m.index("--comments") + 1] == "Jigs"
    assert m[m.index("--series") + 1] == "Wood"
    assert m[m.index("--index") + 1] == "95"
    assert "-a" not in m and "--isbn" not in m


def test_backup_original_never_overwrites(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"v1"); backup_original(str(tmp_path), "a.pdf")
    (tmp_path / "a.pdf").write_bytes(b"v2"); p = backup_original(str(tmp_path), "a.pdf")
    assert open(p, "rb").read() == b"v1"
    assert p == os.path.join(str(tmp_path), "_quarantine", "_originals", "a.pdf")


def test_lit_gets_sidecar_only(tmp_path, monkeypatch):
    calls = []; monkeypatch.setattr(writemeta, "run_tool", lambda a, timeout=60: calls.append(a) or "")
    (tmp_path / "x.lit").write_bytes(b"l")
    out = write_metadata(str(tmp_path), "x.lit", Identity(title="Router Table"), "lit", sidecar_all=False)
    assert calls == [] and out == [str(tmp_path / "x.opf")]
    assert "Router Table" in (tmp_path / "x.opf").read_text(encoding="utf-8")


def test_epub_embedded_calls_ebook_meta_and_backs_up(tmp_path, monkeypatch):
    calls = []; monkeypatch.setattr(writemeta, "run_tool", lambda a, timeout=60: calls.append((a, timeout)) or "")
    (tmp_path / "sub").mkdir(); (tmp_path / "sub" / "b.epub").write_bytes(b"e")
    out = write_metadata(str(tmp_path), "sub/b.epub", Identity(title="T"), "epub", sidecar_all=False)
    assert out == [str(tmp_path / "sub" / "b.epub")]
    assert calls[0][1] == 120 and calls[0][0][0] == "ebook-meta"
    assert (tmp_path / "_quarantine" / "_originals" / "sub" / "b.epub").read_bytes() == b"e"


def test_sidecar_all_adds_opf_and_backs_up_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(writemeta, "run_tool", lambda a, timeout=60: "")
    (tmp_path / "b.epub").write_bytes(b"e"); (tmp_path / "b.opf").write_text("old")
    out = write_metadata(str(tmp_path), "b.epub", Identity(title="T"), "epub", sidecar_all=True)
    assert out == [str(tmp_path / "b.epub"), str(tmp_path / "b.opf")]
    assert (tmp_path / "_quarantine" / "_originals" / "b.opf").read_text() == "old"
    assert "<dc:title>T</dc:title>" in (tmp_path / "b.opf").read_text(encoding="utf-8")


def test_pdf_sidecar_includes_cover(tmp_path, monkeypatch):
    monkeypatch.setattr(writemeta, "run_tool", lambda a, timeout=60: "")
    _pdf(str(tmp_path / "m.pdf"))
    out = write_metadata(str(tmp_path), "m.pdf", Identity(title="T"), "pdf", sidecar_all=True)
    assert out == [str(tmp_path / "m.pdf"), str(tmp_path / "m.opf"), str(tmp_path / "m.cover.jpg")]
    assert Image.open(tmp_path / "m.cover.jpg").format == "JPEG"
    assert not (tmp_path / "m.jpg").exists()


def test_cover_failure_skips_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(writemeta, "run_tool", lambda a, timeout=60: "")
    def boom(*a, **k): raise ToolError("x")
    monkeypatch.setattr(writemeta, "render_page_png", boom)
    (tmp_path / "m.pdf").write_bytes(b"x")
    out = write_metadata(str(tmp_path), "m.pdf", Identity(title="T"), "pdf", sidecar_all=True)
    assert out == [str(tmp_path / "m.pdf"), str(tmp_path / "m.opf")]


def test_image_gets_opf_only(tmp_path):
    (tmp_path / "p.jpg").write_bytes(b"x")
    out = write_metadata(str(tmp_path), "p.jpg", Identity(title="T"), "jpg", sidecar_all=True)
    assert out == [str(tmp_path / "p.opf")]


def test_cbz_comicinfo_and_cover(tmp_path):
    p = tmp_path / "c.cbz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("02.png", _png((0, 0, 255))); z.writestr("01.png", _png()); z.writestr("ComicInfo.xml", "<old/>")
    ident = Identity(magazine="Wood", issue_date="2002-04", issue_number=95, cover_story="Jigs & Fixtures", publisher="Pub", authors=["A"])
    out = write_metadata(str(tmp_path), "c.cbz", ident, "cbz", sidecar_all=True)
    assert out == [str(p), str(tmp_path / "c.opf"), str(tmp_path / "c.cover.jpg")]
    with zipfile.ZipFile(p) as z:
        assert sorted(z.namelist()) == ["01.png", "02.png", "ComicInfo.xml"]
        xml = z.read("ComicInfo.xml").decode()
    for frag in ("<Title>Wood - April 2002</Title>", "<Series>Wood</Series>", "<Number>95</Number>",
                 "<Year>2002</Year>", "<Month>4</Month>", "<Summary>Jigs &amp; Fixtures</Summary>",
                 "<Publisher>Pub</Publisher>", "<Writer>A</Writer>"):
        assert frag in xml
    assert (tmp_path / "_quarantine" / "_originals" / "c.cbz").exists()
    cover = Image.open(tmp_path / "c.cover.jpg")
    assert cover.getpixel((1, 1))[0] > 150  # first by name = 01.png (red)
    assert not list(tmp_path.glob("*.tmp*"))


@pytest.mark.tools
def test_real_ebook_meta_on_pdf(tmp_path):
    _pdf(str(tmp_path / "m.pdf"))
    ident = Identity(magazine="Wood", issue_date="2002-04", issue_number=95)
    write_metadata(str(tmp_path), "m.pdf", ident, "pdf", sidecar_all=False)
    assert "Wood - April 2002" in run_tool(["ebook-meta", str(tmp_path / "m.pdf")])
