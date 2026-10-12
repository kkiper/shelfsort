"""Apply a plan to disk: journaled moves, metadata writing, undo, recovery.

This is the only module that changes the user's files. Files are never
deleted or overwritten: every move is `os.rename` within one source, onto a
path verified free (case-insensitively). Only `empty_quarantine` deletes.

Journal layout: one row per operation (`move`, `meta`, `sidecar`) with the
source NAME in `source` and paths relative to the source root. `move` rows
carry `old_json` = the files row before the move, so undo can restore it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from shelfsort.models import Identity
from shelfsort.mounts import check_source
from shelfsort.plan import _suffixed
from shelfsort.scan import sha256_file
from shelfsort.writemeta import write_metadata

_QUARANTINE = "_quarantine"
_ORIGINALS = "_quarantine/_originals"
_APPLIED = ("organized", "quarantined")


@dataclass
class BatchResult:
    done: int
    skipped: int
    state: str  # done | interrupted


def _full(root: str, relpath: str) -> str:
    return os.path.join(root, *relpath.split("/"))


def _rel(root: str, full: str) -> str:
    return os.path.relpath(full, root).replace(os.sep, "/")


def _name_taken(root: str, relpath: str, ignore_name: str | None = None) -> bool:
    """True if a sibling with the same name (case-insensitive) exists."""
    folder, _, name = relpath.rpartition("/")
    try:
        names = os.listdir(_full(root, folder) if folder else root)
    except (FileNotFoundError, NotADirectoryError):
        return False
    key = name.casefold()
    return any(n.casefold() == key and n != ignore_name for n in names)


# ---------------------------------------------------------------- create


def create_batch(conn: sqlite3.Connection, scope: dict) -> int:
    where = ["f.status not in ('organized', 'quarantined')"]
    args: list = []
    if scope.get("source"):
        where.append("f.source = ?")
        args.append(scope["source"])
    for key in ("subject", "kind"):
        if scope.get(key):
            where.append(f"p.{key} = ?")
            args.append(scope[key])
    if scope.get("subject") or scope.get("kind"):
        where.append("p.action in ('move', 'rename')")
    rows = conn.execute(
        "select f.id from plan_entries p join files f on f.id = p.file_id "
        f"where {' and '.join(where)} order by f.id",
        args,
    ).fetchall()
    with conn:
        cur = conn.execute(
            "insert into batches(scope, state, created) values (?, 'pending', ?)",
            (json.dumps(scope), datetime.now(timezone.utc).isoformat()),
        )
        bid = cur.lastrowid
        conn.executemany(
            "insert into batch_files(batch_id, file_id) values (?, ?)",
            [(bid, r["id"]) for r in rows],
        )
    return bid


# ------------------------------------------------------------------- run


def _journal(conn, batch_id, op, source, src, dst, state, old_json=None) -> int:
    seq = conn.execute(
        "select coalesce(max(seq), 0) + 1 from journal where batch_id = ?", (batch_id,)
    ).fetchone()[0]
    cur = conn.execute(
        "insert into journal(batch_id, seq, op, src, dst, state, source, old_json) "
        "values (?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, seq, op, src, dst, state, source, old_json),
    )
    return cur.lastrowid


def _review(conn, reason: str, file_id: int | None) -> None:
    conn.execute(
        "insert into review_items(reason, file_id, resolved) values (?, ?, 0)",
        (reason, file_id),
    )


def _best_identity(conn, file_id: int) -> Identity | None:
    rows = conn.execute(
        "select method, json, confidence from identities where file_id = ?", (file_id,)
    ).fetchall()
    if not rows:
        return None
    best = max(rows, key=lambda r: (r["confidence"], r["method"] == "manual"))
    known = Identity.__dataclass_fields__.keys()
    return Identity(**{k: v for k, v in json.loads(best["json"]).items() if k in known})


def _write_meta(conn, batch_id, source, root, f, new_rel, sidecar_all) -> None:
    ident = _best_identity(conn, f["id"])
    if ident is None:
        return
    try:
        written = write_metadata(root, new_rel, ident, f["ext"], sidecar_all)
    except Exception:
        _journal(conn, batch_id, "meta", source, None, new_rel, "failed")
        _review(conn, "meta_failed", f["id"])
        conn.commit()
        return
    target_abs = os.path.abspath(_full(root, new_rel))
    for path in written:
        rel = _rel(root, path)
        backup = f"{_ORIGINALS}/{rel}"
        backup_src = backup if os.path.exists(_full(root, backup)) else None
        op = "meta" if os.path.abspath(path) == target_abs else "sidecar"
        _journal(conn, batch_id, op, source, backup_src, rel, "done")
    conn.commit()


def _update_file_row(conn, file_id: int, root: str, rel: str, status: str) -> None:
    full = _full(root, rel)
    st = os.stat(full)
    conn.execute(
        "update files set relpath = ?, size = ?, mtime = ?, sha256 = ?, status = ? "
        "where id = ?",
        (rel, st.st_size, st.st_mtime, sha256_file(full), status, file_id),
    )


def run_batch(conn, batch_id: int, sources: dict[str, str], settings) -> BatchResult:
    batch = conn.execute("select * from batches where id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ValueError(f"unknown batch {batch_id}")
    if batch["state"] not in ("pending", "interrupted"):
        raise ValueError(f"batch {batch_id} is {batch['state']}; cannot run")
    rows = conn.execute(
        "select f.*, p.action, p.target_relpath, p.write_meta from batch_files b "
        "join files f on f.id = b.file_id "
        "left join plan_entries p on p.file_id = f.id "
        "where b.batch_id = ? order by f.id",
        (batch_id,),
    ).fetchall()
    done = skipped = 0
    for f in rows:
        if f["status"] in _APPLIED:
            continue  # applied by an earlier run of this (resumed) batch
        root = sources[f["source"]]
        status = check_source(root)
        if not (status.mounted and status.writable):
            with conn:
                conn.execute("update batches set state = 'interrupted' where id = ?", (batch_id,))
            return BatchResult(done, skipped, "interrupted")
        try:
            st = os.stat(_full(root, f["relpath"]))
            unchanged = (st.st_size, st.st_mtime) == (f["size"], f["mtime"])
        except OSError:
            unchanged = False
        if f["action"] is None or not unchanged:
            with conn:
                conn.execute("update files set status = 'review' where id = ?", (f["id"],))
                if f["action"] is not None:
                    _review(conn, "source_changed", f["id"])
            skipped += 1
            continue
        if _apply_entry(conn, batch_id, f["source"], root, f, settings.sidecar_all):
            done += 1
        else:
            skipped += 1
    with conn:
        conn.execute("update batches set state = 'done' where id = ?", (batch_id,))
    return BatchResult(done, skipped, "done")


def _apply_entry(conn, batch_id, source, root, f, sidecar_all) -> bool:
    old = json.dumps({
        "file_id": f["id"], "relpath": f["relpath"], "size": f["size"],
        "mtime": f["mtime"], "sha256": f["sha256"],
    })
    src_rel, dst_rel = f["relpath"], f["target_relpath"]
    quarantine = f["action"] == "quarantine"
    jid = _journal(conn, batch_id, "move", source, src_rel, dst_rel, "pending", old)
    conn.commit()  # journaled before anything touches the disk
    try:
        os.makedirs(os.path.dirname(_full(root, dst_rel)), exist_ok=True)
        # A case-only rename lists the source itself; that is not a collision.
        same_dir = src_rel.rpartition("/")[0] == dst_rel.rpartition("/")[0]
        own_name = src_rel.rpartition("/")[2] if same_dir else None
        n, final = 1, dst_rel
        while _name_taken(root, final, own_name):
            n += 1
            final = _suffixed(dst_rel, n)
        if final != dst_rel:
            conn.execute("update journal set dst = ? where id = ?", (final, jid))
            conn.commit()
        os.rename(_full(root, src_rel), _full(root, final))
    except OSError:
        with conn:
            conn.execute("update journal set state = 'failed' where id = ?", (jid,))
            conn.execute("update files set status = 'review' where id = ?", (f["id"],))
            _review(conn, "move_failed", f["id"])
        return False
    conn.execute("update journal set state = 'done' where id = ?", (jid,))
    conn.commit()
    if f["write_meta"] and not quarantine:
        _write_meta(conn, batch_id, source, root, f, final, sidecar_all)
    with conn:
        _update_file_row(conn, f["id"], root, final, "quarantined" if quarantine else "organized")
    return True


# ------------------------------------------------------------------ undo


def _copy_over(src: str, dst: str) -> None:
    """Replace dst with a copy of src atomically (temp copy beside dst)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".undo-", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def undo_batch(conn, batch_id: int, sources: dict[str, str]) -> int:
    batch = conn.execute("select state from batches where id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ValueError(f"unknown batch {batch_id}")
    if batch["state"] == "final":
        raise ValueError("batch is final (quarantine emptied); undo is unavailable")
    rows = conn.execute(
        "select * from journal where batch_id = ? and state = 'done' order by seq desc",
        (batch_id,),
    ).fetchall()
    undone = 0
    for j in rows:
        root = sources[j["source"]]
        backup = _full(root, j["src"]) if j["src"] else None
        if j["op"] == "sidecar":
            dst = _full(root, j["dst"])
            if os.path.exists(dst):
                os.remove(dst)
            if backup and os.path.exists(backup):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(backup, dst)
        elif j["op"] == "meta":
            if backup and os.path.exists(backup):
                _copy_over(backup, _full(root, j["dst"]))
        elif j["op"] == "move":
            if not _undo_move(conn, j, root):
                continue
        conn.execute("update journal set state = 'undone' where id = ?", (j["id"],))
        conn.commit()
        undone += 1
    with conn:
        conn.execute("update batches set state = 'undone' where id = ?", (batch_id,))
    return undone


def _undo_move(conn, j, root: str) -> bool:
    old = json.loads(j["old_json"]) if j["old_json"] else None
    src_full, dst_full = _full(root, j["src"]), _full(root, j["dst"])
    if _name_taken(root, j["src"]) or not os.path.exists(dst_full):
        with conn:
            conn.execute("update journal set state = 'failed' where id = ?", (j["id"],))
            _review(conn, "undo_conflict", old["file_id"] if old else None)
        return False
    os.makedirs(os.path.dirname(src_full), exist_ok=True)
    os.rename(dst_full, src_full)
    if old:
        conn.execute(
            "update files set relpath = ?, size = ?, mtime = ?, sha256 = ?, "
            "status = 'planned' where id = ?",
            (old["relpath"], old["size"], old["mtime"], old["sha256"], old["file_id"]),
        )
    return True


# --------------------------------------------------------------- recover


def recover(conn, sources: dict[str, str]) -> int:
    """Settle journal rows left `pending` by a crash. Returns rows changed."""
    fixed = 0
    rows = conn.execute("select * from journal where state = 'pending' order by id").fetchall()
    for j in rows:
        root = sources.get(j["source"])
        if root is None:
            continue
        old = json.loads(j["old_json"]) if j["old_json"] else None
        file_id = old["file_id"] if old else None
        is_move = j["op"] == "move"
        src_exists = is_move and os.path.exists(_full(root, j["src"]))
        dst_exists = j["dst"] is not None and os.path.exists(_full(root, j["dst"]))
        with conn:
            if is_move and dst_exists and not src_exists:
                conn.execute("update journal set state = 'done' where id = ?", (j["id"],))
                if old:
                    quarantined = j["dst"].startswith(_QUARANTINE + "/")
                    _update_file_row(conn, file_id, root, j["dst"],
                                     "quarantined" if quarantined else "organized")
            elif is_move and src_exists:
                conn.execute("update journal set state = 'failed' where id = ?", (j["id"],))
            else:
                conn.execute("update journal set state = 'failed' where id = ?", (j["id"],))
                _review(conn, "recover_lost", file_id)
        fixed += 1
    return fixed


# ------------------------------------------------------ empty quarantine


def empty_quarantine(conn, sources: dict[str, str], source_name: str, confirm: str) -> int:
    """Delete everything under <root>/_quarantine (the only deleting code)."""
    if confirm != "EMPTY":
        raise ValueError('confirmation must be "EMPTY"')
    qdir = os.path.join(sources[source_name], _QUARANTINE)
    deleted = 0
    if os.path.isdir(qdir):
        for entry in os.listdir(qdir):
            path = os.path.join(qdir, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                deleted += sum(len(files) for _, _, files in os.walk(path))
                shutil.rmtree(path)
            else:
                deleted += 1
                os.remove(path)
    with conn:
        conn.execute(
            "update batches set state = 'final' where id in "
            "(select distinct batch_id from journal where source = ?)",
            (source_name,),
        )
    return deleted
