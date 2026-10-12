from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from shelfsort.models import FileStatus

HANDLED_EXTS = frozenset(
    "pdf epub mobi azw azw3 lit fb2 djvu cbz cbr jpg jpeg png".split()
)

# Directories skipped at the source root only.
_SKIPPED_ROOT_DIRS = frozenset({"_quarantine", ".shelfsort"})
_CHUNK = 1024 * 1024


@dataclass
class ScanResult:
    added: int
    updated: int
    unchanged: int
    missing: int


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: str):
    """Yield (relpath with forward slashes, absolute path) for files under root."""
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.normpath(dirpath) == os.path.normpath(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIPPED_ROOT_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield rel, full


def scan_sources(
    conn: sqlite3.Connection,
    sources: dict[str, str],
    progress: Callable[[int], None] | None = None,
) -> ScanResult:
    added = updated = unchanged = missing = 0
    seen_count = 0
    for source, root in sources.items():
        existing = {
            row["relpath"]: row
            for row in conn.execute("select * from files where source = ?", (source,))
        }
        seen: set[str] = set()
        for rel, full in _walk(root):
            try:
                st = os.stat(full)
            except OSError:
                continue  # vanished or unreadable; treated as not seen
            ext = os.path.splitext(rel)[1].lower().lstrip(".")
            handled = ext in HANDLED_EXTS
            row = existing.get(rel)
            unchanged_file = (
                row is not None
                and row["size"] == st.st_size
                and row["mtime"] == st.st_mtime
            )
            digest = None
            if handled and not unchanged_file:
                try:
                    digest = sha256_file(full)
                except OSError:
                    continue  # vanished, locked or denied; treated as not seen
            seen.add(rel)
            seen_count += 1
            if row is None:
                conn.execute(
                    "insert into files (source, relpath, size, mtime, sha256, ext, status)"
                    " values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        source, rel, st.st_size, st.st_mtime, digest, ext,
                        (FileStatus.NEW if handled else FileStatus.IGNORED).value,
                    ),
                )
                added += 1
            elif unchanged_file:
                if row["status"] == FileStatus.MISSING.value:
                    # Reappeared: re-evaluate it from scratch.
                    conn.execute(
                        "update files set status = ? where id = ?",
                        ((FileStatus.NEW if handled else FileStatus.IGNORED).value,
                         row["id"]))
                unchanged += 1
            else:
                status = (FileStatus.NEW if handled else FileStatus.IGNORED).value
                conn.execute(
                    "update files set size = ?, mtime = ?, sha256 = ?, ext = ?,"
                    " pages = null, status = ? where id = ?",
                    (st.st_size, st.st_mtime, digest, ext, status, row["id"]),
                )
                updated += 1
            if progress:
                progress(seen_count)
        gone = [row for rel, row in existing.items() if rel not in seen]
        missing += len(gone)
        # Quarantined rows live under the skipped _quarantine folder.
        conn.executemany(
            "update files set status = ? where id = ?",
            [(FileStatus.MISSING.value, row["id"]) for row in gone
             if row["status"] not in (FileStatus.QUARANTINED.value,
                                      FileStatus.MISSING.value)])
    conn.commit()
    return ScanResult(added, updated, unchanged, missing)
