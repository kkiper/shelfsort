import pytest

from shelfsort import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


@pytest.fixture
def add_file(conn):
    def _add(source, relpath, sha=None, status="new"):
        ext = relpath.rsplit(".", 1)[-1].lower() if "." in relpath else ""
        cur = conn.execute(
            "insert into files(source, relpath, size, mtime, sha256, ext, status) "
            "values (?, ?, 1, 0.0, ?, ?, ?)",
            (source, relpath, sha, ext, status),
        )
        conn.commit()
        return cur.lastrowid

    return _add
