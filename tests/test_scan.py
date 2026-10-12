import os

import pytest

from shelfsort import scan
from shelfsort.scan import HANDLED_EXTS, scan_sources


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    return d


def rows_by_path(conn):
    return {row["relpath"]: row for row in conn.execute("select * from files")}


def test_scan_hashes_and_ignores(src, conn):
    (src / "a.pdf").write_bytes(b"x")
    (src / "notes.txt").write_text("y")
    (src / "_quarantine").mkdir()
    (src / "_quarantine" / "old.pdf").write_bytes(b"z")
    (src / ".shelfsort").mkdir()
    (src / ".shelfsort" / "meta.pdf").write_bytes(b"m")
    r = scan_sources(conn, {"remote": str(src)})
    rows = rows_by_path(conn)
    assert set(rows) == {"a.pdf", "notes.txt"}
    assert rows["notes.txt"]["status"] == "ignored" and rows["notes.txt"]["sha256"] is None
    assert rows["a.pdf"]["sha256"] and rows["a.pdf"]["status"] == "new"
    assert rows["a.pdf"]["ext"] == "pdf" and rows["a.pdf"]["source"] == "remote"
    assert (r.added, r.updated, r.unchanged, r.missing) == (2, 0, 0, 0)


def test_nested_relpath_forward_slashes_and_lower_ext(src, conn):
    (src / "sub" / "deeper").mkdir(parents=True)
    (src / "sub" / "deeper" / "B.EPUB").write_bytes(b"e")
    (src / "sub" / "_quarantine").mkdir()
    (src / "sub" / "_quarantine" / "keep.pdf").write_bytes(b"k")
    scan_sources(conn, {"r": str(src)})
    rows = rows_by_path(conn)
    assert set(rows) == {"sub/deeper/B.EPUB", "sub/_quarantine/keep.pdf"}
    assert rows["sub/deeper/B.EPUB"]["ext"] == "epub"


def test_handled_exts():
    assert HANDLED_EXTS == frozenset(
        "pdf epub mobi azw azw3 lit fb2 djvu cbz cbr jpg jpeg png".split()
    )


def test_rescan_skips_unchanged(src, conn, monkeypatch):
    (src / "a.pdf").write_bytes(b"x")
    scan_sources(conn, {"r": str(src)})
    calls = []
    monkeypatch.setattr(scan, "sha256_file", lambda p: calls.append(p) or "h")
    r = scan_sources(conn, {"r": str(src)})
    assert calls == [] and r.unchanged == 1 and r.added == 0


def test_changed_file_rehashed_and_status_reset(src, conn):
    f = src / "a.pdf"
    f.write_bytes(b"x")
    scan_sources(conn, {"r": str(src)})
    old = rows_by_path(conn)["a.pdf"]["sha256"]
    conn.execute("update files set status='identified', pages=7")
    conn.commit()
    f.write_bytes(b"changed!")
    r = scan_sources(conn, {"r": str(src)})
    row = rows_by_path(conn)["a.pdf"]
    assert r.updated == 1 and row["sha256"] != old and row["size"] == 8
    assert row["status"] == "new" and row["pages"] is None


def test_mtime_change_alone_triggers_update(src, conn):
    f = src / "a.pdf"
    f.write_bytes(b"x")
    scan_sources(conn, {"r": str(src)})
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 100))
    r = scan_sources(conn, {"r": str(src)})
    assert r.updated == 1


def test_updated_ignored_file_stays_ignored(src, conn):
    f = src / "n.txt"
    f.write_text("a")
    scan_sources(conn, {"r": str(src)})
    f.write_text("bbb")
    r = scan_sources(conn, {"r": str(src)})
    row = rows_by_path(conn)["n.txt"]
    assert r.updated == 1 and row["status"] == "ignored" and row["sha256"] is None


def test_missing_kept_and_reappearing_is_unchanged(src, conn):
    f = src / "a.pdf"
    f.write_bytes(b"x")
    scan_sources(conn, {"r": str(src)})
    conn.execute("update files set status='planned'")
    conn.commit()
    data = f.read_bytes()
    st = f.stat()
    f.unlink()
    r = scan_sources(conn, {"r": str(src)})
    assert r.missing == 1 and r.unchanged == 0
    assert rows_by_path(conn)["a.pdf"]["status"] == "planned"
    f.write_bytes(data)
    os.utime(f, (st.st_atime, st.st_mtime))
    r = scan_sources(conn, {"r": str(src)})
    assert r.unchanged == 1 and r.missing == 0 and r.updated == 0


def test_sources_are_independent_and_progress(src, tmp_path, conn):
    other = tmp_path / "other"
    other.mkdir()
    (src / "a.pdf").write_bytes(b"x")
    (other / "a.pdf").write_bytes(b"x")
    seen = []
    r = scan_sources(conn, {"one": str(src), "two": str(other)}, progress=seen.append)
    assert r.added == 2
    assert conn.execute("select count(*) from files").fetchone()[0] == 2
    assert seen and seen[-1] == 2


def test_sha256_file_chunks(tmp_path):
    import hashlib

    p = tmp_path / "big.bin"
    data = os.urandom(2 * 1024 * 1024 + 5)
    p.write_bytes(data)
    assert scan.sha256_file(str(p)) == hashlib.sha256(data).hexdigest()


def test_hash_oserror_skips_file_and_commits_rest(src, conn, monkeypatch):
    (src / "bad.pdf").write_bytes(b"b")
    (src / "good.pdf").write_bytes(b"g")
    real = scan.sha256_file

    def flaky(path):
        if path.endswith("bad.pdf"):
            raise PermissionError("locked")
        return real(path)

    monkeypatch.setattr(scan, "sha256_file", flaky)
    r = scan_sources(conn, {"r": str(src)})
    assert r.added == 1
    conn.rollback()  # anything uncommitted would vanish
    assert set(rows_by_path(conn)) == {"good.pdf"}
