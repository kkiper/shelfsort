import hashlib
import json
import os

import pytest

from shelfsort import apply
from shelfsort.apply import create_batch, empty_quarantine, recover, run_batch, undo_batch
from shelfsort.models import Kind
from shelfsort.mounts import SourceStatus
from shelfsort.plan import PlanInput, build_plan
from shelfsort.scan import scan_sources


def snapshot(root, ignore=frozenset()):
    out = {}
    for dp, dns, fns in os.walk(root):
        if os.path.normpath(dp) == os.path.normpath(root):
            dns[:] = [d for d in dns if d not in ignore]
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            with open(p, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def plan_and_batch(conn, src, scope=None):
    scan_sources(conn, src.sources)
    rows = conn.execute("select * from files where status != 'ignored' order by id").fetchall()
    decisions = [
        PlanInput(r["id"], r["source"], r["relpath"], r["ext"], Kind.PLAN,
                  r["relpath"].split("/")[0], None, False)
        for r in rows
    ]
    build_plan(conn, decisions, src.sources)
    return create_batch(conn, scope or {})


@pytest.fixture(autouse=True)
def no_metadata(monkeypatch):
    monkeypatch.setattr(apply, "write_metadata", lambda *a, **k: [])


def file_row(conn, relpath):
    return conn.execute("select * from files where relpath=?", (relpath,)).fetchone()


def count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_apply_then_undo_restores_tree(fixture_source, conn, settings):
    before = snapshot(fixture_source.root)
    bid = plan_and_batch(conn, fixture_source)
    assert run_batch(conn, bid, fixture_source.sources, settings).state == "done"
    assert snapshot(fixture_source.root) != before
    assert fixture_source.exists("Woodworking/Plans & Articles/All Weather Morris Chair3.pdf")
    assert undo_batch(conn, bid, fixture_source.sources) == 4
    assert snapshot(fixture_source.root, ignore={".shelfsort", "_quarantine"}) == before
    assert count(conn, "select count(*) from batches where state='undone'") == 1


def test_files_rows_updated_and_restored_after_undo(fixture_source, conn, settings):
    scan_sources(conn, fixture_source.sources)
    orig = dict(file_row(conn, "Gardening/compost.pdf"))
    bid = plan_and_batch(conn, fixture_source)
    run_batch(conn, bid, fixture_source.sources, settings)
    new = conn.execute("select * from files where id=?", (orig["id"],)).fetchone()
    assert new["relpath"] == "Gardening/Plans & Articles/Compost.pdf"
    assert new["status"] == "organized" and new["sha256"] == orig["sha256"]
    undo_batch(conn, bid, fixture_source.sources)
    back = dict(conn.execute("select * from files where id=?", (orig["id"],)).fetchone())
    assert back["status"] == "planned"
    assert {k: v for k, v in back.items() if k != "status"} == {
        k: v for k, v in orig.items() if k != "status"}


def test_mount_loss_interrupts(fixture_source, conn, settings, monkeypatch):
    bid = plan_and_batch(conn, fixture_source)
    n = {"i": 0}

    def flaky(root):
        n["i"] += 1
        return SourceStatus(n["i"] < 3, True, "")

    monkeypatch.setattr(apply, "check_source", flaky)
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.state == "interrupted" and r.done == 2 and n["i"] == 3
    assert count(conn, "select count(*) from batches where state='interrupted'") == 1
    # resume (mount is back) finishes the remaining entries only
    monkeypatch.setattr(apply, "check_source", lambda root: SourceStatus(True, True, ""))
    r2 = run_batch(conn, bid, fixture_source.sources, settings)
    assert r2.state == "done" and r2.done == 2
    assert count(conn, "select count(*) from journal where op='move'") == 4


def test_changed_source_is_skipped(fixture_source, conn, settings):
    bid = plan_and_batch(conn, fixture_source)
    os.remove(fixture_source.path("Woodworking/All-weather-Morris-Chair3.pdf"))
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.skipped == 1 and r.state == "done" and r.done == 3
    assert file_row(conn, "Woodworking/All-weather-Morris-Chair3.pdf")["status"] == "review"
    assert count(conn, "select count(*) from review_items where reason='source_changed'") == 1


def test_modified_source_is_skipped(fixture_source, conn, settings):
    bid = plan_and_batch(conn, fixture_source)
    fixture_source.write("Gardening/compost.pdf", b"changed content")
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.skipped == 1 and fixture_source.exists("Gardening/compost.pdf")


def test_quarantine_target_exists_is_suffixed(fixture_source, conn, settings, batch_with_entry):
    fixture_source.write("_quarantine/Woodworking/dup.pdf", b"old")
    fixture_source.write("Woodworking/dup.pdf", b"new")
    bid = batch_with_entry(fixture_source, "Woodworking/dup.pdf", action="quarantine")
    run_batch(conn, bid, fixture_source.sources, settings)
    assert fixture_source.read("_quarantine/Woodworking/dup.pdf") == b"old"
    assert fixture_source.read("_quarantine/Woodworking/dup (2).pdf") == b"new"
    j = conn.execute("select dst, state from journal where op='move'").fetchone()
    assert (j["dst"], j["state"]) == ("_quarantine/Woodworking/dup (2).pdf", "done")
    assert file_row(conn, "_quarantine/Woodworking/dup (2).pdf")["status"] == "quarantined"


def test_collision_is_case_insensitive(fixture_source, conn, settings, batch_with_entry):
    fixture_source.write("Out/Same.pdf", b"existing")
    fixture_source.write("In/a.pdf", b"mine")
    bid = batch_with_entry(fixture_source, "In/a.pdf", target="Out/same.pdf")
    run_batch(conn, bid, fixture_source.sources, settings)
    assert fixture_source.read("Out/Same.pdf") == b"existing"
    assert fixture_source.read("Out/same (2).pdf") == b"mine"


def test_create_batch_scope(fixture_source, conn):
    scan_sources(conn, fixture_source.sources)
    ids = {r["relpath"]: r["id"] for r in conn.execute("select id, relpath from files")}
    rows = [
        (ids["Woodworking/shop_stool.pdf"], "move", "W/x.pdf", "Plans & Articles", "Woodworking"),
        (ids["Gardening/compost.pdf"], "rename", "G/y.pdf", "Books", "Gardening"),
        (ids["Gardening/herb-notes.pdf"], "quarantine", "_quarantine/Gardening/h.pdf", None, None),
    ]
    for fid, action, tgt, kind, subj in rows:
        conn.execute("insert into plan_entries values (?,?,?,?,?,?)", (fid, action, tgt, 0, kind, subj))
    done_id = ids["Woodworking/All-weather-Morris-Chair3.pdf"]
    conn.execute("insert into plan_entries values (?, 'move', 'z.pdf', 0, 'Books', 'Woodworking')", (done_id,))
    conn.execute("update files set status='organized' where id=?", (done_id,))
    conn.commit()

    def members(scope):
        b = create_batch(conn, scope)
        return {r[0] for r in conn.execute("select file_id from batch_files where batch_id=?", (b,))}

    assert members({}) == {r[0] for r in rows}
    assert members({"subject": "Gardening"}) == {ids["Gardening/compost.pdf"]}
    assert members({"kind": "Plans & Articles"}) == {ids["Woodworking/shop_stool.pdf"]}
    assert members({"source": "remote"}) == {r[0] for r in rows}
    assert members({"source": "other"}) == set()
    b = create_batch(conn, {"source": "remote"})
    row = conn.execute("select scope, state from batches where id=?", (b,)).fetchone()
    assert json.loads(row["scope"]) == {"source": "remote"} and row["state"] == "pending"


def test_recover_pending(conn, fixture_source, insert_journal, journal_state):
    fixture_source.write("new/a.pdf", b"x")  # dst exists, src gone
    jid = insert_journal(state="pending", src="old/a.pdf", dst="new/a.pdf")
    fixture_source.write("old/b.pdf", b"y")  # src still there
    jid2 = insert_journal(state="pending", src="old/b.pdf", dst="new/b.pdf")
    jid3 = insert_journal(state="pending", src="old/c.pdf", dst="new/c.pdf")  # neither
    assert recover(conn, fixture_source.sources) == 3
    assert journal_state(jid) == "done" and journal_state(jid2) == "failed"
    assert journal_state(jid3) == "failed"
    assert count(conn, "select count(*) from review_items") == 1
    assert recover(conn, fixture_source.sources) == 0


def test_recover_repairs_files_row(fixture_source, conn, settings, batch_with_entry):
    bid = batch_with_entry(fixture_source, "Gardening/compost.pdf", target="G/compost.pdf")
    run_batch(conn, bid, fixture_source.sources, settings)
    # simulate a crash right after the rename: journal pending, files row stale
    conn.execute("update journal set state='pending'")
    conn.execute("update files set relpath='Gardening/compost.pdf', status='planned' where relpath='G/compost.pdf'")
    conn.commit()
    assert recover(conn, fixture_source.sources) == 1
    row = file_row(conn, "G/compost.pdf")
    assert row["status"] == "organized"


def test_empty_quarantine_requires_confirm(conn, fixture_source):
    fixture_source.write("_quarantine/x.pdf", b"x")
    with pytest.raises(ValueError):
        empty_quarantine(conn, fixture_source.sources, "remote", "yes")
    assert fixture_source.exists("_quarantine/x.pdf")


def test_empty_quarantine_deletes_and_blocks_undo(fixture_source, conn, settings, batch_with_entry):
    bid = batch_with_entry(fixture_source, "Woodworking/shop_stool.pdf", action="quarantine")
    run_batch(conn, bid, fixture_source.sources, settings)
    fixture_source.write("_quarantine/_originals/o.pdf", b"o")
    assert empty_quarantine(conn, fixture_source.sources, "remote", "EMPTY") == 2
    assert os.path.isdir(fixture_source.path("_quarantine"))
    assert os.listdir(fixture_source.path("_quarantine")) == []
    assert fixture_source.exists("Woodworking/All-weather-Morris-Chair3.pdf")
    assert conn.execute("select state from batches where id=?", (bid,)).fetchone()[0] == "final"
    with pytest.raises(ValueError):
        undo_batch(conn, bid, fixture_source.sources)


def test_undo_conflict_when_original_reoccupied(fixture_source, conn, settings):
    bid = plan_and_batch(conn, fixture_source)
    run_batch(conn, bid, fixture_source.sources, settings)
    fixture_source.write("Gardening/compost.pdf", b"someone elses file")
    assert undo_batch(conn, bid, fixture_source.sources) == 3
    assert fixture_source.read("Gardening/compost.pdf") == b"someone elses file"
    assert fixture_source.exists("Gardening/Plans & Articles/Compost.pdf")
    assert count(conn, "select count(*) from journal where state='failed'") == 1
    assert count(conn, "select count(*) from review_items where reason='undo_conflict'") == 1
    assert file_row(conn, "Gardening/Plans & Articles/Compost.pdf")["status"] == "organized"


def test_meta_failed_continues_batch(fixture_source, conn, settings, monkeypatch):
    bid = plan_and_batch(conn, fixture_source)
    fid = file_row(conn, "Gardening/compost.pdf")["id"]
    conn.execute("insert into identities(file_id, method, json, confidence) "
                 "values (?, 'x', '{\"title\": \"T\"}', 0.9)", (fid,))
    conn.commit()

    def boom(*a, **k):
        raise RuntimeError("ebook-meta failed")

    monkeypatch.setattr(apply, "write_metadata", boom)
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.state == "done" and r.done == 4
    assert count(conn, "select count(*) from journal where op='meta' and state='failed'") == 1
    assert conn.execute("select count(*) from review_items where reason='meta_failed' "
                        "and file_id=?", (fid,)).fetchone()[0] == 1
    assert conn.execute("select status from files where id=?", (fid,)).fetchone()[0] == "organized"


def test_best_identity_is_used_and_manual_wins_ties(fixture_source, conn, settings, monkeypatch):
    bid = plan_and_batch(conn, fixture_source)
    fid = file_row(conn, "Gardening/compost.pdf")["id"]
    for method, title, conf in [("isbn", "Low", 0.5), ("embedded", "Tie", 0.9), ("manual", "Manual", 0.9)]:
        conn.execute("insert into identities(file_id, method, json, confidence) values (?,?,?,?)",
                     (fid, method, json.dumps({"title": title}), conf))
    conn.commit()
    seen = []
    monkeypatch.setattr(apply, "write_metadata",
                        lambda root, rel, ident, ext, sc: seen.append((rel, ident.title, ext, sc)) or [])
    run_batch(conn, bid, fixture_source.sources, settings)
    assert seen == [("Gardening/Plans & Articles/Compost.pdf", "Manual", "pdf", True)]


def test_metadata_undo_restores_bytes_and_removes_sidecar(fixture_source, conn, settings, monkeypatch):
    from shelfsort.writemeta import backup_original

    def fake(root, rel, ident, ext, sidecar_all):
        full = os.path.join(root, *rel.split("/"))
        backup_original(root, rel)
        with open(full, "ab") as f:
            f.write(b"EMBEDDED")
        side_rel = os.path.splitext(rel)[0] + ".opf"
        side = os.path.join(root, *side_rel.split("/"))
        if os.path.exists(side):
            backup_original(root, side_rel)
        with open(side, "wb") as f:
            f.write(b"<opf/>")
        return [full, side]

    fixture_source.write("Gardening/Plans & Articles/Compost.opf", b"old sidecar")
    before = snapshot(fixture_source.root)
    bid = plan_and_batch(conn, fixture_source)
    for r in conn.execute("select id from files where status != 'ignored'").fetchall():
        conn.execute("insert into identities(file_id, method, json, confidence) "
                     "values (?, 'm', '{}', 0.9)", (r["id"],))
    conn.commit()
    monkeypatch.setattr(apply, "write_metadata", fake)
    run_batch(conn, bid, fixture_source.sources, settings)
    assert fixture_source.read("Woodworking/Plans & Articles/Shop Stool.pdf") == b"stoolEMBEDDED"
    ops = [r[0] for r in conn.execute("select op from journal order by id")]
    assert ops.count("meta") == 4 and ops.count("sidecar") == 4
    row = file_row(conn, "Woodworking/Plans & Articles/Shop Stool.pdf")
    assert row["sha256"] == hashlib.sha256(b"stoolEMBEDDED").hexdigest()
    undo_batch(conn, bid, fixture_source.sources)
    assert snapshot(fixture_source.root, ignore={".shelfsort", "_quarantine"}) == before
    assert not fixture_source.exists("Woodworking/Plans & Articles/Shop Stool.opf")
    assert fixture_source.read("Gardening/Plans & Articles/Compost.opf") == b"old sidecar"


def test_run_batch_rejects_finished_batch(fixture_source, conn, settings):
    bid = plan_and_batch(conn, fixture_source)
    run_batch(conn, bid, fixture_source.sources, settings)
    with pytest.raises(ValueError):
        run_batch(conn, bid, fixture_source.sources, settings)
