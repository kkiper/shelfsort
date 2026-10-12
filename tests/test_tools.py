import sys

import pytest

from shelfsort import pdftext, tools
from shelfsort.tools import ToolError, ToolTimeout, ebook_meta_read, run_tool


def test_run_tool_returns_stdout():
    assert run_tool([sys.executable, "-c", "import sys; sys.stdout.buffer.write('h\u00e9llo'.encode('utf-8'))"]) == "héllo"


def test_run_tool_timeout():
    with pytest.raises(ToolTimeout):
        run_tool([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)


def test_run_tool_nonzero():
    with pytest.raises(ToolError, match="boom"):
        run_tool([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])


def test_run_tool_missing_binary():
    with pytest.raises(ToolError):
        run_tool(["definitely-not-a-real-binary-xyz"])


def test_ebook_meta_read_parses_output(monkeypatch):
    out = (
        "Title               : Router Table\r\n"
        "Author(s)           : Bill Hylton [Hylton, Bill]\r\n"
        "Publisher           : Fox Chapel\r\n"
        "Published           : 2012-05-01T00:00:00+00:00\r\n"
        "Identifiers         : isbn:9781462923229\r\n"
        "Comments            : first line\r\n"
        "                      second line\r\n"
    )
    seen = {}

    def fake(args, timeout=60):
        seen["args"] = args
        return out

    monkeypatch.setattr(tools, "run_tool", fake)
    d = ebook_meta_read("/x/book.epub")
    assert seen["args"] == ["ebook-meta", "/x/book.epub"]
    assert d["title"] == "Router Table"
    assert d["author(s)"] == "Bill Hylton [Hylton, Bill]"
    assert d["identifiers"] == "isbn:9781462923229"
    assert d["published"].startswith("2012")
    assert d["comments"] == "first line\nsecond line"


def _make_pdf(path, n=3):
    import pymupdf

    doc = pymupdf.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"Hello page {i}")
    doc.save(str(path))
    doc.close()


def test_pdftext_on_generated_pdf(tmp_path):
    p = tmp_path / "g.pdf"
    _make_pdf(p)
    assert pdftext.page_count(str(p)) == 3
    assert "Hello page 1" in pdftext.page_text(str(p), [1])
    both = pdftext.page_text(str(p), [0, 2, 99, -1])
    assert "Hello page 0" in both and "Hello page 2" in both
    assert pdftext.render_page_png(str(p), 0, dpi=50).startswith(b"\x89PNG")


def test_pdftext_corrupt_raises_toolerror(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not a pdf at all")
    with pytest.raises(ToolError):
        pdftext.page_count(str(p))
    with pytest.raises(ToolError):
        pdftext.page_text(str(p), [0])
    with pytest.raises(ToolError):
        pdftext.render_page_png(str(p), 0)


def test_pdftext_render_out_of_range(tmp_path):
    p = tmp_path / "g.pdf"
    _make_pdf(p, 1)
    with pytest.raises(ToolError):
        pdftext.render_page_png(str(p), 5)


@pytest.mark.tools
def test_ebook_meta_real_pdf(tmp_path):
    import pymupdf

    p = tmp_path / "m.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.set_metadata({"title": "Router Table", "author": "Bill Hylton"})
    doc.save(str(p))
    d = ebook_meta_read(str(p))
    assert d["title"] == "Router Table"
    assert d["author(s)"] == "Bill Hylton"
