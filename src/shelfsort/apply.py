"""Apply a plan to disk: journaled moves, metadata writing, undo, recovery.

This is the only module that changes the user's files. Files are never
deleted or overwritten: every move is `os.rename` within one source, onto a
path verified free (case-insensitively). Undo also never deletes or
overwrites: anything it would displace is moved to `_quarantine/_undone`.
Only `empty_quarantine` deletes.

Journal layout: one row per operation (`move`, `meta`) with the source NAME
in `source` and paths relative to the source root. `old_json` holds:
- move rows: the files row before the move (file_id, relpath, size, mtime,
  sha256) so undo can restore it;
- meta rows: file_id, the expected sidecars with whether each pre-existed,
  the unique backup_dir (`_quarantine/_originals/b<batch>/j<journal id>`) and
  the paths written.
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


@dataclass
class BatchResult:
    done: int
    skipped: int
    state: str  # done | interrupted


class SourceUnavailable(ValueError):
    """A source the operation touches is not mounted/writable; nothing changed."""


def _full(root: str, relpath: str) -> str:
    return os.path.join(root, *relpath.split("/"))


def _rel(root: str, full: str) -> str:
    return os.path.relpath(full, root).replace(os.sep, "/")


def _listing(root: str, relpath: str) -> list[str]:
    folder = relpath.rpartition("/")[0]
    try:
        return os.listdir(_full(root, folder) if folder else root)
    except (FileNotFoundError, NotADirectoryError):
        return []


def _name_taken(root: str, relpath: str, ignore_name: str | None = None) -> bool:
    """True if a sibling with the same name (case-insensitive) exists."""
    key = relpath.rpartition("/")[2].casefold()
    return any(n.casefold() == key and n != ignore_name for n in _listing(root, relpath))


def _exists_exact(root: str, relpath: str) -> bool:
    """Exists under exactly this name (case-sensitive, even on case-folding disks)."""
    return relpath.rpartition("/")[2] in _listing(root, relpath)


def _usable(root: str | None) -> bool:
    if root is None:
        return False
    st = check_source(root)
    return st.mounted and st.writable


# ---------------------------------------------------------------- create


def create_batch(conn: sqlite3.Connection, scope: dict) -> int:
    where = ["f.status not in ('organized', 'quarantined', 'review', 'missing')"]
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
        ids = [(r["id"],) for r in rows]
        conn.executemany(
            "insert into batch_files(batch_id, file_id) values (?, ?)",
            [(bid, r["id"]) for r in rows],
        )
        conn.executemany("update files set status = 'planned' where id = ?", ids)
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


def best_identity(conn, file_id: int) -> Identity | None:
    rows = conn.execute(
        "select method, json, confidence from identities where file_id = ?", (file_id,)
    ).fetchall()
    if not rows:
        return None
    best = max(rows, key=lambda r: (r["confidence"], r["method"] == "manual"))
    known = Identity.__dataclass_fields__.keys()
    return Identity(**{k: v for k, v in json.loads(best["json"]).items() if k in known})


def _write_meta(conn, batch_id, source, root, f, new_rel, sidecar_all) -> None:
    ident = best_identity(conn, f["id"])
    if ident is None:
        return
    stem = os.path.splitext(new_rel)[0]
    info = {
        "file_id": f["id"],
        "size": os.stat(_full(root, new_rel)).st_size,  # pre-write size
        "sidecars": [
            {"rel": rel, "existed": os.path.lexists(_full(root, rel)),
             "size": os.path.getsize(_full(root, rel)) if os.path.lexists(_full(root, rel)) else None}
            for rel in (stem + ".opf", stem + ".cover.jpg")
        ],
        "written": [],
        "backups": {},
    }
    jid = _journal(conn, batch_id, "meta", source, None, new_rel, "pending", json.dumps(info))
    info["backup_dir"] = f"{_ORIGINALS}/b{batch_id}/j{jid}"
    conn.execute("update journal set old_json = ? where id = ?", (json.dumps(info), jid))
    conn.commit()  # journaled before the file is touched
    state = "failed"
    try:
        written = write_metadata(root, new_rel, ident, f["ext"], sidecar_all,
                                 backup_dir=info["backup_dir"])
        info["written"] = [_rel(root, p) for p in written]
        state = "done"
    except Exception:
        _review(conn, "meta_failed", f["id"])
    finally:
        info["backups"] = {rel: sha256_file(p) for rel, p in _backup_files(root, info["backup_dir"])}
        conn.execute("update journal set state = ?, old_json = ? where id = ?",
                     (state, json.dumps(info), jid))
        conn.commit()


def _backup_files(root: str, backup_dir: str):
    """Yield (rel, abs path) of complete backups (temp files excluded)."""
    base = _full(root, backup_dir)
    for dp, _, fns in os.walk(base):
        for fn in fns:
            if fn.startswith(".bak-") and fn.endswith(".tmp"):
                continue
            yield _rel(base, os.path.join(dp, fn)), os.path.join(dp, fn)


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
        if f["status"] != "planned":
            continue  # applied or already skipped by an earlier run of this batch
        root = sources.get(f["source"])
        if not _usable(root):
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
        if f["action"] == "quarantine" and not _keeper_present(conn, f, sources):
            with conn:
                conn.execute("update files set status = 'review' where id = ?", (f["id"],))
                _review(conn, "keeper_missing", f["id"])
            skipped += 1
            continue
        if _apply_entry(conn, batch_id, f["source"], root, f, settings.sidecar_all):
            done += 1
        else:
            skipped += 1
    with conn:
        conn.execute("update batches set state = 'done' where id = ?", (batch_id,))
    return BatchResult(done, skipped, "done")


def _keeper_present(conn, f, sources: dict[str, str]) -> bool:
    """For an exact duplicate, is its group's keeper (same sha) on disk at full size?

    Quarantine entries that are not exact duplicates (review decisions) pass.
    """
    keepers = conn.execute(
        "select k.* from group_members d "
        "join groups g on g.id = d.group_id and g.type = 'exact' "
        "join group_members km on km.group_id = g.id and km.decision = 'keep' "
        "join files k on k.id = km.file_id "
        "where d.file_id = ? and d.decision = 'quarantine'", (f["id"],)).fetchall()
    for k in keepers:
        root = sources.get(k["source"])
        if (k["sha256"] != f["sha256"] or k["status"] in ("missing", "quarantined")
                or not _usable(root)):
            return False
        try:
            if os.stat(_full(root, k["relpath"])).st_size != k["size"]:
                return False
        except OSError:
            return False
    return True


def _apply_entry(conn, batch_id, source, root, f, sidecar_all) -> bool:
    old = json.dumps({
        "file_id": f["id"], "relpath": f["relpath"], "size": f["size"],
        "mtime": f["mtime"], "sha256": f["sha256"],
    })
    src_rel, dst_rel = f["relpath"], f["target_relpath"]
    quarantine = f["action"] == "quarantine"
    status = "quarantined" if quarantine else "organized"
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
    with conn:  # journal "done" and the files row change together
        conn.execute("update journal set state = 'done' where id = ?", (jid,))
        conn.execute("update files set relpath = ?, status = ? where id = ?",
                     (final, status, f["id"]))
    if f["write_meta"] and not quarantine:
        _write_meta(conn, batch_id, source, root, f, final, sidecar_all)
    with conn:  # refresh size/mtime/sha after the meta step
        _update_file_row(conn, f["id"], root, final, status)
    return True


# ------------------------------------------------------------------ undo


def _unique(root: str, rel: str) -> str:
    n, cand = 1, rel
    while os.path.lexists(_full(root, cand)):
        n += 1
        cand = _suffixed(rel, n)
    return cand


def _set_aside(root: str, jid: int, rel: str) -> None:
    """Move root/rel to _quarantine/_undone/j<jid>/<rel> (never delete)."""
    dest = _unique(root, f"{_QUARANTINE}/_undone/j{jid}/{rel}")
    os.makedirs(os.path.dirname(_full(root, dest)), exist_ok=True)
    os.rename(_full(root, rel), _full(root, dest))


def _copy_back(src: str, dst: str) -> None:
    """Copy src to dst via a temp file beside dst, then os.replace."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".undo-", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _backups_valid(j, root: str, info: dict) -> bool:
    """Check each backup against its recorded sha, else the recorded pre-write size."""
    sizes = {j["dst"]: info.get("size")}
    sizes.update({sc["rel"]: sc.get("size") for sc in info.get("sidecars", [])})
    for rel, path in _backup_files(root, info["backup_dir"]):
        sha = info.get("backups", {}).get(rel)
        if sha is not None:
            if sha256_file(path) != sha:
                return False
        elif sizes.get(rel) is None or os.path.getsize(path) != sizes[rel]:
            return False
    return True


def _undo_meta(conn, j, root: str) -> bool:
    info = json.loads(j["old_json"]) if j["old_json"] else {}
    bdir = info.get("backup_dir")
    has_backups = bool(bdir) and os.path.isdir(_full(root, bdir))
    if has_backups and not _backups_valid(j, root, info):
        with conn:
            conn.execute("update journal set state = 'failed' where id = ?", (j["id"],))
            _review(conn, "backup_corrupt", info.get("file_id"))
        return False
    for sc in info.get("sidecars", []):
        if not sc["existed"] and os.path.lexists(_full(root, sc["rel"])):
            _set_aside(root, j["id"], sc["rel"])
    if has_backups:
        for rel, backup in _backup_files(root, bdir):
            if os.path.lexists(_full(root, rel)):
                _set_aside(root, j["id"], rel)
            _copy_back(backup, _full(root, rel))
    return True


def undo_batch(conn, batch_id: int, sources: dict[str, str]) -> int:
    batch = conn.execute("select state from batches where id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ValueError(f"unknown batch {batch_id}")
    if batch["state"] == "final":
        raise ValueError("batch is final (quarantine emptied); undo is unavailable")
    rows = conn.execute(
        "select * from journal where batch_id = ? order by seq desc", (batch_id,)
    ).fetchall()
    # A move whose earlier undo hit a conflict is retried (re-checked) too.
    rows = [j for j in rows if j["state"] in ("done", "undo_conflict")
            or (j["op"] == "meta" and j["state"] in ("failed", "pending"))]
    for name in {j["source"] for j in rows}:
        if not _usable(sources.get(name)):
            raise SourceUnavailable(f"source {name!r} is not mounted and writable")
    undone = failed = 0
    for j in rows:
        root = sources[j["source"]]
        if j["op"] == "meta":
            if not _undo_meta(conn, j, root):
                failed += 1
                continue
        elif j["op"] == "move" and not _undo_move(conn, j, root):
            failed += 1
            continue
        conn.execute("update journal set state = 'undone' where id = ?", (j["id"],))
        conn.commit()
        undone += 1
    state = "partially_undone" if failed else "undone"
    with conn:
        conn.execute("update batches set state = ? where id = ?", (state, batch_id))
    return undone


def _undo_move(conn, j, root: str) -> bool:
    old = json.loads(j["old_json"]) if j["old_json"] else None
    same_dir = j["src"].rpartition("/")[0] == j["dst"].rpartition("/")[0]
    ignore = j["dst"].rpartition("/")[2] if same_dir else None
    if _name_taken(root, j["src"], ignore) or not _exists_exact(root, j["dst"]):
        with conn:
            conn.execute("update journal set state = 'undo_conflict' where id = ?", (j["id"],))
            _review(conn, "undo_conflict", old["file_id"] if old else None)
        return False
    src_full = _full(root, j["src"])
    os.makedirs(os.path.dirname(src_full), exist_ok=True)
    os.rename(_full(root, j["dst"]), src_full)
    if old:
        conn.execute(
            "update files set relpath = ?, size = ?, mtime = ?, sha256 = ?, "
            "status = 'planned' where id = ?",
            (old["relpath"], old["size"], old["mtime"], old["sha256"], old["file_id"]),
        )
    return True


# --------------------------------------------------------------- recover


def recover(conn, sources: dict[str, str]) -> int:
    """Settle journal rows left `pending` by a crash. Returns rows changed.

    Rows of sources that are not mounted stay pending.
    """
    fixed = 0
    usable: dict[str, bool] = {}
    rows = conn.execute("select * from journal where state = 'pending' order by id").fetchall()
    for j in rows:
        name = j["source"]
        if name not in usable:
            usable[name] = _usable(sources.get(name))
        if not usable[name]:
            continue
        root = sources[name]
        info = json.loads(j["old_json"]) if j["old_json"] else None
        file_id = info["file_id"] if info else None
        is_move = j["op"] == "move"
        src_exists = is_move and _exists_exact(root, j["src"])
        dst_exists = j["dst"] is not None and _exists_exact(root, j["dst"])
        with conn:
            if is_move and dst_exists and not src_exists:
                conn.execute("update journal set state = 'done' where id = ?", (j["id"],))
                if info:
                    quarantined = j["dst"].startswith(_QUARANTINE + "/")
                    _update_file_row(conn, file_id, root, j["dst"],
                                     "quarantined" if quarantined else "organized")
            elif is_move and src_exists:
                conn.execute("update journal set state = 'failed' where id = ?", (j["id"],))
            else:
                # lost move, or an interrupted meta write (undo still reverts it)
                conn.execute("update journal set state = 'failed' where id = ?", (j["id"],))
                _review(conn, "meta_failed" if j["op"] == "meta" else "recover_lost", file_id)
        fixed += 1
    with conn:  # a batch the crash left 'pending' mid-run can be resumed or undone
        conn.execute(
            "update batches set state = 'interrupted' where state = 'pending' and id in"
            " (select batch_id from journal)")
    return fixed


# ------------------------------------------------------ empty quarantine


def _is_link(path: str) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(isjunction and isjunction(path))


# Problems whose recovery may still need files under _quarantine.
BLOCKING_PROBLEMS = ("meta_failed", "backup_corrupt", "undo_conflict")


def unresolved_problems(conn, source_name: str) -> int:
    """Unresolved blocking problems of this source (or not tied to any file)."""
    marks = ",".join("?" * len(BLOCKING_PROBLEMS))
    return conn.execute(
        f"select count(*) from review_items r left join files f on f.id = r.file_id"
        f" where r.resolved = 0 and r.reason in ({marks})"
        f" and (r.file_id is null or f.source = ?)",
        (*BLOCKING_PROBLEMS, source_name)).fetchone()[0]


def empty_quarantine(conn, sources: dict[str, str], source_name: str, confirm: str,
                     include_undone: bool = False, force: bool = False) -> int:
    """Delete everything under <root>/_quarantine (the only deleting code).

    `_quarantine/_undone` (files set aside by undo) is kept unless include_undone.
    Refuses while unresolved meta_failed/backup_corrupt/undo_conflict problems
    exist for the source, unless `force` (the user confirmed reviewing them).
    """
    if confirm != "EMPTY":
        raise ValueError('confirmation must be "EMPTY"')
    if not force and (n := unresolved_problems(conn, source_name)):
        raise ValueError(
            f"{n} unresolved apply problem(s) for {source_name!r} may still need the"
            " quarantined backups; review them first, or tick \"I have reviewed apply"
            " problems\"")
    root = sources.get(source_name)
    if not _usable(root):
        raise SourceUnavailable(f"source {source_name!r} is not mounted and writable")
    qdir = os.path.join(root, _QUARANTINE)
    if _is_link(qdir) or (
        os.path.exists(qdir)
        and os.path.realpath(qdir) != os.path.join(os.path.realpath(root), _QUARANTINE)
    ):
        raise ValueError("_quarantine is a link or resolves outside the source; refusing")
    with conn:  # final first: a crash mid-delete must not leave undo available
        conn.execute(
            "update batches set state = 'final' where id in "
            "(select distinct batch_id from journal where source = ?)",
            (source_name,),
        )
    deleted = 0
    if os.path.isdir(qdir):
        for entry in os.listdir(qdir):
            if entry == "_undone" and not include_undone:
                continue
            path = os.path.join(qdir, entry)
            if os.path.isdir(path) and not _is_link(path):
                deleted += sum(len(files) for _, _, files in os.walk(path))
                shutil.rmtree(path)
            else:
                deleted += 1
                os.remove(path)
    return deleted
