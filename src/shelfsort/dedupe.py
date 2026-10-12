import sqlite3
from pathlib import PurePosixPath

from shelfsort.settings import Settings

DEFAULT_VAGUE_FOLDERS = Settings().vague_folders


def _rank(row, local_source: str, vague: set[str]) -> tuple:
    relpath = row["relpath"].replace("\\", "/")
    parts = relpath.split("/")
    top = parts[0].lower() if len(parts) > 1 else ""
    stem = PurePosixPath(relpath).stem
    return (
        row["source"] != local_source,
        not any(ch.isalpha() for ch in stem),
        top in vague,
        len(relpath),
        row["id"],
    )


def find_exact_duplicates(
    conn: sqlite3.Connection,
    local_source: str = "local",
    vague_folders: list[str] = DEFAULT_VAGUE_FOLDERS,
) -> list[tuple[int, list[int]]]:
    vague = {v.lower() for v in vague_folders}
    conn.execute(
        "delete from group_members where group_id in "
        "(select id from groups where type='exact')")
    conn.execute("delete from groups where type='exact'")
    rows = conn.execute(
        "select id, source, relpath, sha256 from files "
        "where sha256 is not null and status != 'ignored' order by id").fetchall()
    by_sha: dict[str, list] = {}
    for row in rows:
        by_sha.setdefault(row["sha256"], []).append(row)
    result = []
    for sha, members in by_sha.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: _rank(r, local_source, vague))
        keeper = members[0]["id"]
        dups = sorted(m["id"] for m in members[1:])
        gid = conn.execute(
            "insert into groups(type, key) values ('exact', ?)", (sha,)).lastrowid
        conn.execute(
            "insert into group_members(group_id, file_id, decision) values (?, ?, 'keep')",
            (gid, keeper))
        conn.executemany(
            "insert into group_members(group_id, file_id, decision) "
            "values (?, ?, 'quarantine')", [(gid, d) for d in dups])
        result.append((keeper, dups))
    conn.commit()
    result.sort(key=lambda t: t[0])
    return result
