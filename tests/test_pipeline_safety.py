"""Pipeline safety: stale rows, unreadable recovery, unmounted sources."""

import os

import pytest

from shelfsort.pipeline import run_scan_pipeline
from shelfsort.settings import Settings


def quarantined(conn):
    return sorted(r[0] for r in conn.execute(
        "select f.source || ':' || f.relpath from plan_entries p join files f"
        " on f.id = p.file_id where p.action = 'quarantine'"))


@pytest.fixture
def run(conn, patch_resolvers):
    patch_resolvers()

    def _run(s):
        return run_scan_pipeline(conn, s, lambda *a: None)

    return _run


def test_renamed_folder_never_quarantines_the_only_copy(conn, tmp_source, run):
    tmp_source.write("Wood/Book.pdf", b"only copy")
    s = Settings(sources=tmp_source.sources)
    run(s)
    os.rename(tmp_source.root / "Wood", tmp_source.root / "Woodworking")
    run(s)
    assert quarantined(conn) == []
    row = conn.execute("select status from files where relpath='Wood/Book.pdf'").fetchone()
    assert row["status"] == "missing"


def test_vanished_local_copy_never_quarantines_remote(conn, tmp_path, run):
    roots = {name: tmp_path / name for name in ("local", "remote")}
    for root in roots.values():
        (root / "Woodworking").mkdir(parents=True)
        (root / "Woodworking" / "Book.pdf").write_bytes(b"same bytes")
    s = Settings(sources={k: str(v) for k, v in roots.items()})
    run(s)
    assert quarantined(conn) == ["remote:Woodworking/Book.pdf"]
    os.remove(roots["local"] / "Woodworking" / "Book.pdf")
    (roots["local"] / "keep.txt").write_bytes(b"k")  # source stays mounted
    run(s)
    assert quarantined(conn) == []
