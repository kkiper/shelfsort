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


# ------------------------------------------------------------- I2

def status_of(conn, relpath):
    return conn.execute("select status from files where relpath=?", (relpath,)).fetchone()[0]


def test_unreadable_file_is_retried_on_rescan(conn, tmp_source, run, monkeypatch):
    from shelfsort import pdftext
    from shelfsort.tools import ToolError

    tmp_source.write("Woodworking/scan.pdf", b"not yet readable")
    calls = []

    def page_count(path):
        calls.append(path)
        if len(calls) == 1:
            raise ToolError("locked by another process")
        return 100

    monkeypatch.setattr(pdftext, "page_count", page_count)
    s = Settings(sources=tmp_source.sources)
    run(s)
    assert status_of(conn, "Woodworking/scan.pdf") == "unreadable"
    run(s)
    assert status_of(conn, "Woodworking/scan.pdf") == "review"
    reasons = [r[0] for r in conn.execute("select reason from review_items")]
    assert reasons == ["unidentified"]


def test_resolved_unreadable_with_manual_identity_is_planned(conn, tmp_source, run):
    import json
    from dataclasses import asdict

    from shelfsort.models import Identity
    from shelfsort.pipeline import replan

    tmp_source.write("Woodworking/broken.pdf", b"corrupt")  # page count always fails
    s = Settings(sources=tmp_source.sources)
    run(s)
    fid, iid = conn.execute(
        "select file_id, id from review_items where reason='unreadable'").fetchone()
    ident = Identity(title="Chair Plans", authors=["Ann Lee"], method="manual", confidence=1.0)
    conn.execute("insert into identities(file_id, method, json, confidence)"
                 " values (?, 'manual', ?, 1.0)", (fid, json.dumps(asdict(ident))))
    conn.execute("update review_items set resolved=1, decision_json=? where id=?",
                 (json.dumps({"subject": "Woodworking", "kind": "Books"}), iid))
    conn.commit()
    replan(conn, s)
    target = conn.execute("select target_relpath from plan_entries where file_id=?",
                          (fid,)).fetchone()
    assert target[0] == "Woodworking/Books/Ann Lee/Chair Plans.pdf"
    run(s)  # a full rescan keeps it planned
    assert status_of(conn, "Woodworking/broken.pdf") == "planned"


def test_unmounted_source_is_skipped_untouched_with_warning(conn, tmp_path, run):
    roots = {name: tmp_path / name for name in ("local", "remote")}
    for root in roots.values():
        (root / "Woodworking").mkdir(parents=True)
        (root / "Woodworking" / f"{root.name}.pdf").write_bytes(root.name.encode())
    s = Settings(sources={k: str(v) for k, v in roots.items()})
    run(s)
    before = conn.execute(
        "select id, status from files where source='remote' order by id").fetchall()
    items = conn.execute("select count(*) from review_items r join files f on f.id=r.file_id"
                         " where f.source='remote'").fetchone()[0]
    # remote "unmounts": its mount point is an empty folder
    os.remove(roots["remote"] / "Woodworking" / "remote.pdf")
    os.rmdir(roots["remote"] / "Woodworking")
    import shutil
    shutil.rmtree(roots["remote"] / ".shelfsort", ignore_errors=True)
    summary = run(s)
    assert [tuple(r) for r in conn.execute(
        "select id, status from files where source='remote' order by id")] == \
        [tuple(r) for r in before]
    assert conn.execute("select count(*) from review_items r join files f on f.id=r.file_id"
                        " where f.source='remote'").fetchone()[0] == items
    assert len(summary.warnings) == 1 and "remote" in summary.warnings[0]


def test_dashboard_shows_last_scan_warnings(client_factory, tmp_path):
    import json

    from shelfsort import db

    client = client_factory(sources={})
    c = db.connect(client.db_path)
    c.execute("insert into settings(key, json) values ('last_summary', ?)", (json.dumps({
        "status_counts": {}, "subject_counts": {}, "kind_counts": {},
        "review_reasons": {}, "warnings": ["Source remote skipped: not mounted"]}),))
    c.commit()
    c.close()
    assert "Source remote skipped: not mounted" in client.get("/").text
