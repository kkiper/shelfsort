import io
import os

import pymupdf
import pytest
from PIL import Image

from shelfsort.models import Identity


def pdf_bytes(pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page().insert_text((72, 72), "hello")
    data = doc.tobytes()
    doc.close()
    return data


def png_bytes(w, h, fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(out, fmt)
    return out.getvalue()


def add_plan(web, relpath, target, action="move", **kw):
    fid = web.add(relpath, **kw)
    web.conn.execute(
        "insert into plan_entries(file_id, action, target_relpath, write_meta, kind, subject)"
        " values (?, ?, ?, 0, 'Books', 'Woodworking')", (fid, action, target))
    web.conn.execute("update files set status='planned' where id=?", (fid,))
    web.conn.commit()
    return fid


# ------------------------------------------------------------------ covers

def test_cover_pdf_rendered_and_cached(web):
    fid = web.add("Woodworking/a.pdf", "sha-a", data=pdf_bytes())
    r = web.client.get(f"/files/{fid}/cover.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    thumb = os.path.join(os.path.dirname(web.client.db_path), "thumbs", "sha-a.png")
    assert os.path.isfile(thumb)
    os.remove(web.src.root / "Woodworking" / "a.pdf")  # cache serves without the file
    assert web.client.get(f"/files/{fid}/cover.png").content == r.content


def test_cover_image_is_thumbnailed(web):
    fid = web.add("Woodworking/big.jpg", "sha-j", data=png_bytes(1200, 900, "JPEG"))
    r = web.client.get(f"/files/{fid}/cover.png")
    img = Image.open(io.BytesIO(r.content))
    assert r.status_code == 200 and img.format == "PNG" and max(img.size) == 600


def test_cover_unsupported_or_corrupt_is_404(web):
    epub = web.add("Woodworking/a.epub", "sha-e")
    bad = web.add("Woodworking/bad.pdf", "sha-b", data=b"not a pdf")
    assert web.client.get(f"/files/{epub}/cover.png").status_code == 404
    assert web.client.get(f"/files/{bad}/cover.png").status_code == 404
    assert web.client.get("/files/9999/cover.png").status_code == 404


def test_cover_refuses_path_escape(web, tmp_path):
    (tmp_path / "outside.png").write_bytes(png_bytes(10, 10))
    cur = web.conn.execute(
        "insert into files(source, relpath, size, mtime, sha256, ext, status)"
        " values ('remote', '../outside.png', 1, 0, 'sha-out', 'png', 'new')")
    web.conn.commit()
    assert web.client.get(f"/files/{cur.lastrowid}/cover.png").status_code == 404
    assert not os.path.exists(os.path.join(os.path.dirname(web.client.db_path), "thumbs", "sha-out.png"))


# -------------------------------------------------------------------- plan

def test_plan_table_and_filters(web):
    add_plan(web, "Woodworking/a.pdf", "Woodworking/Books/Ann/A.pdf")
    f2 = add_plan(web, "Gardening/b.pdf", "Gardening/Books/Bo/B.pdf")
    web.conn.execute("update plan_entries set subject='Gardening' where file_id=?", (f2,))
    web.conn.commit()
    r = web.client.get("/plan")
    assert "Woodworking/Books/Ann/A.pdf" in r.text and "Gardening/Books/Bo/B.pdf" in r.text
    r = web.client.get("/plan?subject=Gardening")
    assert "Gardening/Books/Bo/B.pdf" in r.text and "Woodworking/Books/Ann/A.pdf" not in r.text
    assert "Woodworking/Books/Ann/A.pdf" not in web.client.get("/plan?source=other").text


def test_apply_runs_batch_as_job(web):
    fid = add_plan(web, "Woodworking/a.pdf", "Woodworking/Books/Ann/A.pdf", sha="s")
    r = web.client.post("/apply", data={"source": "remote", "subject": "Woodworking"})
    assert r.status_code == 202
    web.client.app.state.worker.join(10)
    assert web.client.app.state.worker.status().error is None
    assert (web.src.root / "Woodworking" / "Books" / "Ann" / "A.pdf").exists()
    assert not (web.src.root / "Woodworking" / "a.pdf").exists()
    b = web.one("select scope, state from batches")
    assert b["state"] == "done" and '"Woodworking"' in b["scope"]
    assert web.one("select status from files where id=?", fid)[0] == "organized"
    assert "done" in web.client.get("/history").text


def test_apply_rejects_second_job(web, slow_job):
    assert web.client.post("/scan").status_code == 202
    assert web.client.post("/apply").status_code == 409


# ----------------------------------------------------------------- history

def test_undo_runs_as_job(web):
    add_plan(web, "Woodworking/a.pdf", "Woodworking/Books/Ann/A.pdf")
    web.client.post("/apply")
    web.client.app.state.worker.join(10)
    bid = web.one("select id from batches")[0]
    assert web.client.post(f"/history/{bid}/undo").status_code == 202
    web.client.app.state.worker.join(10)
    assert web.client.app.state.worker.status().error is None
    assert (web.src.root / "Woodworking" / "a.pdf").exists()
    assert web.one("select state from batches")[0] == "undone"


def test_undo_with_unmounted_source_surfaces_job_error(client_factory, tmp_path):
    from shelfsort import db

    client = client_factory(sources={"remote": str(tmp_path / "missing")})
    c = db.connect(client.db_path)
    bid = c.execute("insert into batches(scope, state, created) values ('{}', 'done', 'now')").lastrowid
    c.execute("insert into journal(batch_id, seq, op, src, dst, state, source)"
              " values (?, 1, 'move', 'a.pdf', 'b.pdf', 'done', 'remote')", (bid,))
    c.commit()
    c.close()
    assert client.post(f"/history/{bid}/undo").status_code == 202
    client.app.state.worker.join(10)
    r = client.get("/job")
    assert "not mounted" in r.text and "failed" in r.text
    assert client.post("/history/999/undo").status_code == 202  # unknown batch: job error
    client.app.state.worker.join(10)
    assert "unknown batch" in client.get("/job").text


def test_history_lists_batches_with_journal_counts(web):
    add_plan(web, "Woodworking/a.pdf", "Woodworking/Books/Ann/A.pdf")
    web.client.post("/apply")
    web.client.app.state.worker.join(10)
    html = web.client.get("/history").text
    assert "done 1" in html and "/undo" in html


def test_empty_quarantine_needs_typed_confirm(client):
    assert client.post("/quarantine/empty", data={"source": "remote", "confirm": "yes"}).status_code == 400


def test_empty_quarantine_unmounted_source_is_400(client_factory, tmp_path):
    client = client_factory(sources={"remote": str(tmp_path / "missing")})
    r = client.post("/quarantine/empty", data={"source": "remote", "confirm": "EMPTY"})
    assert r.status_code == 400 and "not mounted" in r.text


def test_empty_quarantine_deletes_and_keeps_undone_by_default(web):
    web.src.write("_quarantine/a/old.pdf")
    web.src.write("_quarantine/_undone/keep.pdf")
    r = web.client.post("/quarantine/empty", data={"source": "remote", "confirm": "EMPTY"})
    assert r.status_code == 200 and "1 file(s) deleted" in r.text
    assert not (web.src.root / "_quarantine" / "a").exists()
    assert (web.src.root / "_quarantine" / "_undone" / "keep.pdf").exists()
    r = web.client.post("/quarantine/empty", data={
        "source": "remote", "confirm": "EMPTY", "include_undone": "on"})
    assert r.status_code == 200
    assert not (web.src.root / "_quarantine" / "_undone").exists()


# ---------------------------------------------- I1/I3: history and problems

def seed_batch(web, state, journal_states):
    bid = web.conn.execute(
        "insert into batches(scope, state, created) values ('{}', ?, 'now')", (state,)).lastrowid
    for i, js in enumerate(journal_states, 1):
        web.conn.execute(
            "insert into journal(batch_id, seq, op, src, dst, state, source)"
            " values (?, ?, 'move', 'a', 'b', ?, 'remote')", (bid, i, js))
    web.conn.commit()
    return bid


@pytest.mark.parametrize("state", ["done", "interrupted", "partially_undone"])
def test_history_offers_undo(web, state):
    bid = seed_batch(web, state, ["undone", "undo_conflict", "failed"])
    html = web.client.get("/history").text
    assert f"/history/{bid}/undo" in html
    assert "problems 2" in html


def test_empty_quarantine_blocked_by_unresolved_apply_problems(web):
    fid = web.add("Woodworking/a.pdf")
    web.conn.execute("insert into review_items(reason, file_id, resolved) values ('meta_failed', ?, 0)", (fid,))
    web.conn.commit()
    web.src.write("_quarantine/_originals/b1/j1/Woodworking/a.pdf")
    r = web.client.post("/quarantine/empty", data={"source": "remote", "confirm": "EMPTY"})
    assert r.status_code == 400 and "problem" in r.text.lower()
    assert (web.src.root / "_quarantine" / "_originals").exists()
    assert "I have reviewed apply problems" in web.client.get("/history").text
    r = web.client.post("/quarantine/empty", data={
        "source": "remote", "confirm": "EMPTY", "reviewed_problems": "on"})
    assert r.status_code == 200
    assert not (web.src.root / "_quarantine" / "_originals").exists()


def test_plan_lists_only_planned_files(web):
    add_plan(web, "Woodworking/a.pdf", "Woodworking/Books/Ann/A.pdf")
    held = add_plan(web, "Woodworking/b.pdf", "Woodworking/Books/Ann/B.pdf")
    web.conn.execute("update files set status='review' where id=?", (held,))
    web.conn.commit()
    html = web.client.get("/plan").text
    assert "Woodworking/Books/Ann/A.pdf" in html and "Woodworking/Books/Ann/B.pdf" not in html
