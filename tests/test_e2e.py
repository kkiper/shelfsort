import hashlib
import os

import pytest

from shelfsort import pdftext
from shelfsort.apply import create_batch, run_batch, undo_batch
from shelfsort.identify import magazine
from shelfsort.identify.magazine import OcrWord
from shelfsort.pipeline import run_scan_pipeline
from shelfsort.tools import ToolError
from tests.fixtures import library


def snapshot(root, ignore=frozenset()):
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as f:
                snap[rel] = hashlib.sha256(f.read()).hexdigest()
    return snap


@pytest.fixture
def lib(tmp_path, settings, monkeypatch):
    lib = library.build(tmp_path / "lib")
    settings.sources = lib.sources
    settings.magazines = ["Wood"]

    cover = pdftext.render_page_png(lib.path("Woodworking/011167038.pdf"), 0)
    words = [OcrWord("WOOD", 150, 5), OcrWord("Router", 70, 200),
             OcrWord("Jigs", 70, 200), OcrWord("Issue", 15, 900),
             OcrWord("#95", 15, 900), OcrWord("April", 15, 900),
             OcrWord("2002", 15, 900)]
    # Only the magazine's cover has a masthead; other PDFs OCR to nothing.
    monkeypatch.setattr(magazine, "ocr_words",
                        lambda png: words if png == cover else [])
    monkeypatch.setattr("shelfsort.identify.embedded.ebook_meta_read", lambda p: {})

    def no_results(*a, **k):
        raise ToolError("No results found")

    monkeypatch.setattr("shelfsort.identify.isbn.run_tool", no_results)
    monkeypatch.setattr("shelfsort.apply.write_metadata", lambda *a, **k: [])
    return lib


def test_full_dry_run_then_apply_then_undo(conn, settings, lib):
    before = snapshot(lib.root)
    summary = run_scan_pipeline(conn, settings, lambda *a: None)
    assert snapshot(lib.root) == before  # dry run touches nothing
    assert summary.review_reasons == {
        "variant": 1, "near": 1, "unidentified": 2, "unreadable": 1}
    assert summary.kind_counts == {"Magazines": 1, "Plans & Articles": 2, "Books": 1}
    assert summary.subject_counts == {"Woodworking": 4}
    assert conn.execute(
        "select count(*) from plan_entries where action = 'quarantine'"
    ).fetchone()[0] == 1
    assert summary.status_counts["unreadable"] == 1
    assert summary.status_counts["ignored"] == 1
    bid = create_batch(conn, {"source": "remote"})
    run_batch(conn, bid, lib.sources, settings)
    assert lib.exists("Woodworking/Magazines/Wood/Wood - 2002-04 (#95).pdf")
    assert lib.exists("Woodworking/Plans & Articles/Arch Top Bookends.pdf")
    for rel in ("Woodworking/Router Table.epub", "Woodworking/Router Table.lit",
                "Woodworking/a/Morris Chair.pdf", "Woodworking/b/Morris Chair.pdf",
                "Woodworking/broken.pdf"):
        assert lib.exists(rel), rel  # held files are untouched
    quarantined = [p for p in snapshot(lib.root) if p.startswith("_quarantine/")]
    assert any(p.endswith(("dovetail-sawing-guide.pdf", "saw cuts quick sheet.pdf"))
               for p in quarantined)
    undo_batch(conn, bid, lib.sources)
    assert snapshot(lib.root, ignore={".shelfsort", "_quarantine"}) == before


def test_pipeline_is_idempotent(conn, settings, lib):
    stages = []
    first = run_scan_pipeline(conn, settings, lambda *a: stages.append(a[0]))
    items = conn.execute("select count(*) from review_items").fetchone()[0]
    second = run_scan_pipeline(conn, settings, lambda *a: None)
    assert second == first
    assert conn.execute("select count(*) from review_items").fetchone()[0] == items
    assert list(dict.fromkeys(stages)) == [
        "scan", "dedupe", "pages", "identify", "classify", "groups", "plan"]


def test_collision_is_planned_and_idempotent(conn, settings, lib):
    from shelfsort.scan import scan_sources

    target = lib.root / "Woodworking" / "Plans & Articles"
    library.make_pdf(target / "Chair.pdf", ["already filed"])
    library.make_pdf(lib.root / "Woodworking" / "Chair.pdf", ["new copy"])
    scan_sources(conn, settings.sources)
    conn.execute("update files set status = 'organized' "
                 "where relpath = 'Woodworking/Plans & Articles/Chair.pdf'")
    conn.commit()
    first = run_scan_pipeline(conn, settings, lambda *a: None)
    assert first.review_reasons["collision"] == 1
    second = run_scan_pipeline(conn, settings, lambda *a: None)
    assert second == first
    row = conn.execute(
        "select f.status, p.target_relpath from files f join plan_entries p "
        "on p.file_id = f.id where f.relpath = 'Woodworking/Chair.pdf'").fetchone()
    assert row["status"] == "planned"
    assert row["target_relpath"] == "Woodworking/Plans & Articles/Chair (2).pdf"


def test_vanished_file_is_unreadable_not_fatal(conn, settings, lib, monkeypatch):
    real = pdftext.page_count

    def flaky(path):
        if path.endswith("arch-top-bookends.pdf") or path.endswith("<.pdf"):
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(pdftext, "page_count", flaky)
    summary = run_scan_pipeline(conn, settings, lambda *a: None)
    assert summary.status_counts["unreadable"] == 2
