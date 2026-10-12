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


@pytest.fixture
def fixture_source(tmp_source):
    """tmp_source plus a small tree and path/read/exists helpers."""
    tmp_source.path = lambda rel: str(tmp_source.root.joinpath(*rel.split("/")))
    tmp_source.read = lambda rel: tmp_source.root.joinpath(*rel.split("/")).read_bytes()
    tmp_source.exists = lambda rel: tmp_source.root.joinpath(*rel.split("/")).exists()
    tmp_source.write("Woodworking/All-weather-Morris-Chair3.pdf", b"morris chair")
    tmp_source.write("Woodworking/shop_stool.pdf", b"stool")
    tmp_source.write("Gardening/herb-notes.pdf", b"herbs")
    tmp_source.write("Gardening/compost.pdf", b"compost")
    return tmp_source


@pytest.fixture
def batch_with_entry(conn):
    """Insert a file, a plan entry and a pending batch for it directly."""
    from shelfsort.scan import scan_sources

    def _make(src, relpath, action="move", target=None, write_meta=0):
        scan_sources(conn, src.sources)
        fid = conn.execute("select id from files where source=? and relpath=?",
                           (src.name, relpath)).fetchone()["id"]
        if target is None:
            target = f"_quarantine/{relpath}" if action == "quarantine" else relpath
        conn.execute(
            "insert into plan_entries(file_id, action, target_relpath, write_meta) "
            "values (?, ?, ?, ?)", (fid, action, target, write_meta))
        cur = conn.execute(
            "insert into batches(scope, state, created) values ('{}', 'pending', 'now')")
        conn.execute("insert into batch_files values (?, ?)", (cur.lastrowid, fid))
        conn.execute("update files set status='planned' where id=?", (fid,))
        conn.commit()
        return cur.lastrowid

    return _make


@pytest.fixture
def insert_journal(conn):
    def _insert(state="pending", src=None, dst=None, source="remote", op="move", batch_id=None):
        if batch_id is None:
            batch_id = conn.execute(
                "insert into batches(scope, state, created) values ('{}', 'done', 'now')"
            ).lastrowid
        cur = conn.execute(
            "insert into journal(batch_id, seq, op, src, dst, state, source) "
            "values (?, 1, ?, ?, ?, ?, ?)", (batch_id, op, src, dst, state, source))
        conn.commit()
        return cur.lastrowid

    return _insert


@pytest.fixture
def journal_state(conn):
    return lambda jid: conn.execute(
        "select state from journal where id=?", (jid,)).fetchone()["state"]


@pytest.fixture
def client_factory(tmp_path):
    """Build a TestClient around a fresh app: client_factory(sources=..., sources_root=...)."""
    from fastapi.testclient import TestClient

    from shelfsort import settings as settings_mod
    from shelfsort.web.app import create_app

    made = []

    def _make(sources=None, sources_root=None):
        db_path = str(tmp_path / f"web{len(made)}.db")
        root = sources_root if sources_root is not None else tmp_path / "no_sources_root"
        if sources is not None:
            c = db.connect(db_path)
            s = settings_mod.load(c)
            s.sources = dict(sources)
            settings_mod.save(c, s)
            c.close()
        client = TestClient(create_app(db_path, str(root)))
        client.db_path = db_path
        made.append(client)
        return client

    yield _make
    for c in made:
        c.app.state.worker.join(10)


@pytest.fixture
def client(client_factory):
    return client_factory()


@pytest.fixture
def slow_job(monkeypatch):
    """Make the scan job block until released (released automatically on teardown)."""
    import threading

    from shelfsort.pipeline import PipelineSummary
    from shelfsort.web import routes_dashboard

    release = threading.Event()

    def blocked(conn, s, progress):
        progress("scan", 1, 2)
        release.wait(10)
        return PipelineSummary()

    monkeypatch.setattr(routes_dashboard, "run_scan_pipeline", blocked)
    yield release
    release.set()


@pytest.fixture
def web(client_factory, tmp_source):
    """A web client plus a DB connection and helpers to seed files and re-plan."""
    import json
    import os
    from dataclasses import asdict
    from types import SimpleNamespace

    from shelfsort import pipeline
    from shelfsort.models import Identity
    from shelfsort.settings import Settings

    client = client_factory(sources=tmp_source.sources)
    wconn = db.connect(client.db_path)

    def add(relpath, sha=None, ident=None, pages=None, status="new", data=b"x"):
        path = tmp_source.write(relpath, data)
        st = os.stat(path)
        ext = relpath.rsplit(".", 1)[-1].lower()
        cur = wconn.execute(
            "insert into files(source, relpath, size, mtime, sha256, ext, pages, status) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (tmp_source.name, relpath, st.st_size, st.st_mtime, sha or relpath, ext, pages, status))
        if ident is not None:
            ident.method = ident.method or "embedded"
            wconn.execute(
                "insert into identities(file_id, method, json, confidence) values (?, ?, ?, ?)",
                (cur.lastrowid, ident.method, json.dumps(asdict(ident)), ident.confidence))
        wconn.commit()
        return cur.lastrowid

    def replan():
        return pipeline.replan(wconn, Settings(), tmp_source.sources)

    def one(sql, *args):
        return wconn.execute(sql, args).fetchone()

    yield SimpleNamespace(client=client, conn=wconn, src=tmp_source, add=add,
                          replan=replan, one=one)
    wconn.close()
