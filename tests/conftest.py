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


@pytest.fixture
def settings():
    from shelfsort.settings import Settings

    return Settings()


@pytest.fixture
def rec(conn):
    from shelfsort.models import FileRecord, FileStatus

    cur = conn.execute(
        "insert into files(source, relpath, size, mtime, sha256, ext, status) "
        "values ('src', 'x.pdf', 1, 0.0, null, 'pdf', 'new')"
    )
    conn.commit()
    return FileRecord(id=cur.lastrowid, source="src", relpath="x.pdf", size=1,
                      mtime=0.0, sha256=None, ext="pdf", pages=None,
                      status=FileStatus.NEW)


@pytest.fixture
def patch_resolvers(monkeypatch):
    """Patch the five chain resolvers (and PDF page rendering).

    Keyword values: None -> return None; an exception instance or class
    (AssertionError: fail if called) -> raise it; anything else -> returned.
    Returns the list of resolver names in the order they were called.
    """
    from shelfsort import pdftext
    from shelfsort.identify import annas, embedded, isbn, magazine, ollama

    def install(**values):
        calls = []

        def make(name):
            value = values.get(name)

            def fake(*args, **kwargs):
                calls.append(name)
                if isinstance(value, BaseException) or (
                    isinstance(value, type) and issubclass(value, BaseException)
                ):
                    raise value
                return value

            return fake

        monkeypatch.setattr(annas, "parse_annas", make("annas"))
        monkeypatch.setattr(embedded, "resolve", make("embedded"))
        monkeypatch.setattr(isbn, "resolve", make("isbn"))
        monkeypatch.setattr(magazine, "resolve", make("magazine"))
        monkeypatch.setattr(ollama, "resolve", make("ollama"))
        monkeypatch.setattr(pdftext, "render_page_png", lambda *a, **k: b"png")
        return calls

    return install


@pytest.fixture
def tmp_source(tmp_path):
    """A temp source root registered under the name "remote"."""

    class TmpSource:
        name = "remote"
        root = tmp_path / "remote_root"

        def write(self, relpath, data=b"x"):
            p = self.root / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return p

        @property
        def sources(self):
            return {self.name: str(self.root)}

    src = TmpSource()
    src.root.mkdir()
    return src
