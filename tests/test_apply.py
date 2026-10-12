import hashlib
import json
import os

import pytest

from shelfsort import apply
from shelfsort.apply import SourceUnavailable, create_batch, empty_quarantine, recover, run_batch, undo_batch
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
                        lambda root, rel, ident, ext, sc, backup_dir=None: seen.append((rel, ident.title, ext, sc)) or [])
    run_batch(conn, bid, fixture_source.sources, settings)
    assert seen == [("Gardening/Plans & Articles/Compost.pdf", "Manual", "pdf", True)]


def test_metadata_undo_restores_bytes_and_removes_sidecar(fixture_source, conn, settings, monkeypatch):
    from shelfsort.writemeta import backup_original

    def fake(root, rel, ident, ext, sidecar_all, backup_dir):
        full = os.path.join(root, *rel.split("/"))
        backup_original(root, rel, backup_dir)
        with open(full, "ab") as f:
            f.write(b"EMBEDDED")
        side_rel = os.path.splitext(rel)[0] + ".opf"
        side = os.path.join(root, *side_rel.split("/"))
        if os.path.exists(side):
            backup_original(root, side_rel, backup_dir)
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
    assert ops.count("meta") == 4 and "sidecar" not in ops
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


def _ident(conn, fid):
    conn.execute("insert into identities(file_id, method, json, confidence) "
                 "values (?, 'm', '{}', 0.9)", (fid,))
    conn.commit()


def _fake_writer(raise_after=False):
    from shelfsort.writemeta import backup_original

    def fake(root, rel, ident, ext, sidecar_all, backup_dir):
        full = os.path.join(root, *rel.split("/"))
        backup_original(root, rel, backup_dir)
        with open(full, "ab") as f:
            f.write(b"EMBEDDED")
        if raise_after:
            raise RuntimeError("crash")
        return [full]

    return fake


def test_stale_backup_never_restored_over_other_file(fixture_source, conn, settings, monkeypatch, batch_with_entry):
    fixture_source.write("In/a.pdf", b"original A")
    fixture_source.write("In2/b.pdf", b"original B")
    b1 = batch_with_entry(fixture_source, "In/a.pdf", target="Out/t.pdf", write_meta=1)
    b2 = batch_with_entry(fixture_source, "In2/b.pdf", target="Out/t.pdf", write_meta=1)
    for r in conn.execute("select id from files").fetchall():
        _ident(conn, r["id"])
    monkeypatch.setattr(apply, "write_metadata", _fake_writer())
    run_batch(conn, b1, fixture_source.sources, settings)
    undo_batch(conn, b1, fixture_source.sources)
    assert fixture_source.read("In/a.pdf") == b"original A"
    run_batch(conn, b2, fixture_source.sources, settings)
    assert fixture_source.read("Out/t.pdf") == b"original BEMBEDDED"
    undo_batch(conn, b2, fixture_source.sources)
    assert fixture_source.read("In2/b.pdf") == b"original B"
    assert fixture_source.read("In/a.pdf") == b"original A"


def test_crash_after_pending_meta_row_is_undoable(fixture_source, conn, settings, monkeypatch, batch_with_entry):
    bid = batch_with_entry(fixture_source, "Gardening/compost.pdf", target="G/c.pdf", write_meta=1)
    _ident(conn, file_row(conn, "Gardening/compost.pdf")["id"])
    monkeypatch.setattr(apply, "write_metadata", _fake_writer(raise_after=True))
    run_batch(conn, bid, fixture_source.sources, settings)
    assert fixture_source.read("G/c.pdf") == b"compostEMBEDDED"
    conn.execute("update journal set state='pending' where op='meta'")  # as after a real crash
    conn.commit()
    undo_batch(conn, bid, fixture_source.sources)
    assert fixture_source.read("Gardening/compost.pdf") == b"compost"
    assert not fixture_source.exists("G/c.pdf")


def test_undo_refuses_when_unmounted(fixture_source, conn, settings, monkeypatch):
    bid = plan_and_batch(conn, fixture_source)
    run_batch(conn, bid, fixture_source.sources, settings)
    after = snapshot(fixture_source.root)
    monkeypatch.setattr(apply, "check_source", lambda root: SourceStatus(False, False, "gone"))
    with pytest.raises(SourceUnavailable):
        undo_batch(conn, bid, fixture_source.sources)
    assert isinstance(SourceUnavailable("x"), ValueError)
    assert snapshot(fixture_source.root) == after
    assert count(conn, "select count(*) from journal where state != 'done'") == 0
    assert count(conn, "select count(*) from batches where state='done'") == 1


def test_recover_leaves_unmounted_pending(conn, fixture_source, insert_journal, journal_state, monkeypatch):
    fixture_source.write("new/a.pdf", b"x")
    jid = insert_journal(state="pending", src="old/a.pdf", dst="new/a.pdf")
    monkeypatch.setattr(apply, "check_source", lambda root: SourceStatus(False, False, "gone"))
    assert recover(conn, fixture_source.sources) == 0
    assert journal_state(jid) == "pending"


def test_undo_case_only_rename(fixture_source, conn, settings, batch_with_entry):
    bid = batch_with_entry(fixture_source, "Gardening/compost.pdf", target="Gardening/Compost.pdf")
    run_batch(conn, bid, fixture_source.sources, settings)
    assert "Compost.pdf" in os.listdir(fixture_source.path("Gardening"))
    assert undo_batch(conn, bid, fixture_source.sources) == 1
    assert "compost.pdf" in os.listdir(fixture_source.path("Gardening"))
    assert count(conn, "select count(*) from journal where state='failed'") == 0


def test_recover_case_only_rename_uses_actual_name(conn, fixture_source, insert_journal, journal_state):
    fixture_source.write("D/Compost.pdf", b"x")  # renamed already; src name differs only by case
    jid = insert_journal(state="pending", src="D/compost.pdf", dst="D/Compost.pdf")
    assert recover(conn, fixture_source.sources) == 1
    assert journal_state(jid) == "done"


def test_empty_quarantine_refuses_symlinked_quarantine(conn, fixture_source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.pdf").write_bytes(b"keep")
    try:
        os.symlink(outside, fixture_source.path("_quarantine"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    with pytest.raises(ValueError):
        empty_quarantine(conn, fixture_source.sources, "remote", "EMPTY")
    assert (outside / "precious.pdf").read_bytes() == b"keep"


def test_undo_sets_aside_user_replaced_file(fixture_source, conn, settings, monkeypatch, batch_with_entry):
    bid = batch_with_entry(fixture_source, "Gardening/compost.pdf", target="G/c.pdf", write_meta=1)
    _ident(conn, file_row(conn, "Gardening/compost.pdf")["id"])
    monkeypatch.setattr(apply, "write_metadata", _fake_writer())
    run_batch(conn, bid, fixture_source.sources, settings)
    fixture_source.write("G/c.pdf", b"user edited this")
    undo_batch(conn, bid, fixture_source.sources)
    assert fixture_source.read("Gardening/compost.pdf") == b"compost"
    kept = [os.path.join(dp, fn) for dp, _, fns in os.walk(fixture_source.path("_quarantine/_undone"))
            for fn in fns]
    assert [open(k, "rb").read() for k in kept] == [b"user edited this"]


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_empty_quarantine_refuses_junction(conn, fixture_source, tmp_path):
    import subprocess

    outside = tmp_path / "outside_j"
    outside.mkdir()
    (outside / "precious.pdf").write_bytes(b"keep")
    r = subprocess.run(["cmd", "/c", "mklink", "/J", fixture_source.path("_quarantine"), str(outside)],
                       capture_output=True)
    if r.returncode != 0:
        pytest.skip("junction creation not possible")
    with pytest.raises(ValueError):
        empty_quarantine(conn, fixture_source.sources, "remote", "EMPTY")
    assert (outside / "precious.pdf").read_bytes() == b"keep"


def _backup_paths(root):
    return [os.path.join(dp, fn) for dp, _, fns in os.walk(os.path.join(root, "_quarantine", "_originals"))
            for fn in fns]


def _applied_with_meta(fixture_source, conn, settings, monkeypatch, batch_with_entry):
    bid = batch_with_entry(fixture_source, "Gardening/compost.pdf", target="G/c.pdf", write_meta=1)
    _ident(conn, file_row(conn, "Gardening/compost.pdf")["id"])
    monkeypatch.setattr(apply, "write_metadata", _fake_writer())
    run_batch(conn, bid, fixture_source.sources, settings)
    return bid


def test_truncated_backup_is_never_restored_sha(fixture_source, conn, settings, monkeypatch, batch_with_entry):
    bid = _applied_with_meta(fixture_source, conn, settings, monkeypatch, batch_with_entry)
    [bk] = _backup_paths(str(fixture_source.root))
    with open(bk, "wb") as f:
        f.write(b"comp")  # truncated
    undo_batch(conn, bid, fixture_source.sources)
    assert fixture_source.read("Gardening/compost.pdf") == b"compostEMBEDDED"  # untouched
    assert not fixture_source.exists("_quarantine/_undone")
    assert count(conn, "select count(*) from review_items where reason='backup_corrupt'") == 1
    assert count(conn, "select count(*) from journal where op='meta' and state='failed'") == 1


def test_truncated_backup_is_never_restored_size_only(fixture_source, conn, settings, monkeypatch, batch_with_entry):
    bid = _applied_with_meta(fixture_source, conn, settings, monkeypatch, batch_with_entry)
    row = conn.execute("select id, old_json from journal where op='meta'").fetchone()
    info = json.loads(row["old_json"])
    info["backups"] = {}  # as after a crash: no sha recorded
    conn.execute("update journal set state='pending', old_json=? where id=?", (json.dumps(info), row["id"]))
    conn.commit()
    [bk] = _backup_paths(str(fixture_source.root))
    with open(bk, "wb") as f:
        f.write(b"comp")
    undo_batch(conn, bid, fixture_source.sources)
    assert fixture_source.read("Gardening/compost.pdf") == b"compostEMBEDDED"
    assert not fixture_source.exists("_quarantine/_undone")
    assert count(conn, "select count(*) from review_items where reason='backup_corrupt'") == 1


def test_size_only_valid_backup_is_restored(fixture_source, conn, settings, monkeypatch, batch_with_entry):
    bid = _applied_with_meta(fixture_source, conn, settings, monkeypatch, batch_with_entry)
    row = conn.execute("select id, old_json from journal where op='meta'").fetchone()
    info = json.loads(row["old_json"])
    info["backups"] = {}
    conn.execute("update journal set state='pending', old_json=? where id=?", (json.dumps(info), row["id"]))
    conn.commit()
    undo_batch(conn, bid, fixture_source.sources)
    assert fixture_source.read("Gardening/compost.pdf") == b"compost"


def test_backup_is_atomic_no_temp_left(tmp_path):
    from shelfsort.writemeta import backup_original

    (tmp_path / "a.pdf").write_bytes(b"abc")
    dest = backup_original(str(tmp_path), "a.pdf", "bk/j1")
    assert open(dest, "rb").read() == b"abc"
    assert os.listdir(os.path.dirname(dest)) == ["a.pdf"]


def test_empty_quarantine_keeps_undone_by_default(conn, fixture_source):
    fixture_source.write("_quarantine/_undone/j1/x.pdf", b"u")
    fixture_source.write("_quarantine/q.pdf", b"q")
    assert empty_quarantine(conn, fixture_source.sources, "remote", "EMPTY") == 1
    assert fixture_source.exists("_quarantine/_undone/j1/x.pdf") and not fixture_source.exists("_quarantine/q.pdf")
    assert empty_quarantine(conn, fixture_source.sources, "remote", "EMPTY", include_undone=True) == 1
    assert not fixture_source.exists("_quarantine/_undone")


def test_empty_quarantine_unknown_source(conn, fixture_source):
    with pytest.raises(SourceUnavailable):
        empty_quarantine(conn, fixture_source.sources, "nope", "EMPTY")


def test_create_batch_skips_review_files(fixture_source, conn):
    bid = plan_and_batch(conn, fixture_source)
    conn.execute("update files set status='review' where relpath='Gardening/compost.pdf'")
    conn.commit()
    b2 = create_batch(conn, {})
    assert count(conn, f"select count(*) from batch_files where batch_id={b2}") == 3
    assert file_row(conn, "Gardening/compost.pdf")["status"] == "review"
    assert bid != b2


def test_unknown_source_mid_batch_interrupts(fixture_source, conn, settings):
    bid = plan_and_batch(conn, fixture_source)
    cur = conn.execute("insert into files(source, relpath, size, mtime, ext, status) "
                       "values ('other', 'z.pdf', 1, 0.0, 'pdf', 'planned')")
    conn.execute("insert into plan_entries(file_id, action, target_relpath) values (?, 'move', 'Z/z.pdf')",
                 (cur.lastrowid,))
    conn.execute("insert into batch_files values (?, ?)", (bid, cur.lastrowid))
    conn.commit()
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.state == "interrupted" and r.done == 4
    assert count(conn, "select count(*) from batches where state='interrupted'") == 1


def test_resume_does_not_reprocess_review_file(fixture_source, conn, settings):
    bid = plan_and_batch(conn, fixture_source)
    os.remove(fixture_source.path("Gardening/compost.pdf"))
    assert run_batch(conn, bid, fixture_source.sources, settings).skipped == 1
    conn.execute("update batches set state='interrupted'")
    conn.commit()
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert (r.done, r.skipped) == (0, 0)
    assert count(conn, "select count(*) from review_items where reason='source_changed'") == 1


# ------------------------------------------------- C1: exact-dup keeper check

def exact_dup_batch(conn, src):
    from shelfsort.dedupe import find_exact_duplicates

    src.write("Woodworking/Book.pdf", b"same")
    src.write("Downloads/Book.pdf", b"same")
    scan_sources(conn, src.sources)
    [(keeper, [dup])] = find_exact_duplicates(conn)
    conn.execute(
        "insert into plan_entries(file_id, action, target_relpath, write_meta) "
        "values (?, 'quarantine', '_quarantine/Downloads/Book.pdf', 0)", (dup,))
    conn.commit()
    return keeper, dup, create_batch(conn, {"source": "remote"})


def test_quarantine_skipped_when_keeper_missing(fixture_source, conn, settings):
    keeper, dup, bid = exact_dup_batch(conn, fixture_source)
    os.remove(fixture_source.path("Woodworking/Book.pdf"))
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert (r.done, r.skipped) == (0, 1)
    assert fixture_source.read("Downloads/Book.pdf") == b"same"
    assert not fixture_source.exists("_quarantine/Downloads/Book.pdf")
    assert conn.execute("select status from files where id=?", (dup,)).fetchone()[0] == "review"
    assert count(conn, "select count(*) from review_items where reason='keeper_missing'") == 1


def test_quarantine_skipped_when_keeper_size_differs(fixture_source, conn, settings):
    keeper, dup, bid = exact_dup_batch(conn, fixture_source)
    fixture_source.write("Woodworking/Book.pdf", b"edited since the scan")
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.skipped == 1 and fixture_source.exists("Downloads/Book.pdf")


def test_quarantine_proceeds_when_keeper_present(fixture_source, conn, settings):
    keeper, dup, bid = exact_dup_batch(conn, fixture_source)
    r = run_batch(conn, bid, fixture_source.sources, settings)
    assert r.done == 1 and fixture_source.exists("_quarantine/Downloads/Book.pdf")
