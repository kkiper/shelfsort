from __future__ import annotations

import os
import posixpath
import sqlite3
from dataclasses import dataclass

from shelfsort.models import Identity, Kind, PlanEntry
from shelfsort.names import sanitize_component

_MAX = 200
_QUARANTINE = "_quarantine"


@dataclass
class PlanInput:
    file_id: int
    source: str
    relpath: str
    ext: str
    kind: Kind
    subject: str
    ident: Identity | None
    quarantine: bool


def _clean_filename_title(filename: str) -> str:
    stem = posixpath.splitext(filename)[0]
    single_case = stem.lower() == stem or stem.upper() == stem
    text = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return text.title() if single_case else text


def target_relpath(
    kind: Kind, subject: str, ident: Identity | None, filename: str, ext: str
) -> str:
    ext = ext.lower().lstrip(".")
    ident = ident or Identity()
    parts = [sanitize_component(subject), sanitize_component(kind.value)]
    if kind is Kind.BOOK:
        author = ident.authors[0] if ident.authors else "Unknown Author"
        title = ident.title or _clean_filename_title(filename)
        stem = title + (f" ({ident.year})" if ident.year else "")
        parts.append(sanitize_component(author))
    elif kind is Kind.MAGAZINE:
        mag = ident.magazine or "Unknown Magazine"
        stem = f"{mag} - {ident.issue_date or 'undated'}"
        if ident.issue_number:
            stem += f" (#{ident.issue_number})"
        parts.append(sanitize_component(mag))
    else:
        stem = ident.title or _clean_filename_title(filename)
    parts.append(sanitize_component(f"{stem}.{ext}" if ext else stem))
    return "/".join(parts)


def _split(relpath: str) -> tuple[str, str]:
    folder, _, name = relpath.rpartition("/")
    return folder, name


def _suffixed(relpath: str, n: int) -> str:
    folder, name = _split(relpath)
    stem, ext = posixpath.splitext(name)
    suffix = f" ({n})"
    stem = stem[: max(1, _MAX - len(ext) - len(suffix))].rstrip(". ")
    return (folder + "/" if folder else "") + stem + suffix + ext


def build_plan(
    conn: sqlite3.Connection,
    decisions: list[PlanInput],
    sources: dict[str, str],
) -> list[PlanEntry]:
    candidates: list[tuple[PlanInput, str]] = []
    for d in decisions:
        if d.source not in sources:
            raise KeyError(f"unknown source: {d.source!r}")
        if d.quarantine:
            target = f"{_QUARANTINE}/{d.relpath}"
        else:
            target = target_relpath(
                d.kind, d.subject, d.ident, _split(d.relpath)[1], d.ext
            )
        candidates.append((d, target))

    # Files already in place stay put; every other current path is freed.
    freed: dict[str, set[str]] = {}
    for d, target in candidates:
        if target != d.relpath:
            freed.setdefault(d.source, set()).add(d.relpath.casefold())

    listings: dict[tuple[str, str], set[str]] = {}

    def on_disk(source: str, relpath: str) -> bool:
        root = sources[source]
        folder = _split(relpath)[0]
        key = (source, folder)
        if key not in listings:
            names: set[str] = set()
            prefix = folder + "/" if folder else ""
            try:
                with os.scandir(os.path.join(root, *folder.split("/"))) as it:
                    names = {(prefix + e.name).casefold() for e in it}
            except (FileNotFoundError, NotADirectoryError):
                pass
            listings[key] = names
        return relpath.casefold() in listings[key]

    assigned: dict[str, set[str]] = {}
    entries: list[PlanEntry] = []
    collided: list[int] = []
    for d, target in candidates:
        taken = assigned.setdefault(d.source, set())
        if target == d.relpath:  # already in place: no entry
            taken.add(target.casefold())
            continue
        freed_here = freed.get(d.source, set())

        def busy(rel: str) -> bool:
            key = rel.casefold()
            return key in taken or (
                key not in freed_here and on_disk(d.source, rel)
            )

        final, n = target, 1
        while busy(final):
            n += 1
            final = _suffixed(target, n)
        taken.add(final.casefold())
        if final == d.relpath:  # already sits at its suffixed name
            continue
        if d.quarantine:
            action = "quarantine"
        else:
            if final != target:
                collided.append(d.file_id)
            same_dir = _split(final)[0] == _split(d.relpath)[0]
            action = "rename" if same_dir else "move"
        entries.append(
            PlanEntry(
                file_id=d.file_id,
                action=action,
                target_relpath=final,
                write_meta=action != "quarantine",
                kind=d.kind,
                subject=d.subject,
            )
        )

    ids = [(d.file_id,) for d in decisions]
    with conn:  # one transaction; rolled back on exception
        conn.executemany("delete from plan_entries where file_id = ?", ids)
        conn.executemany(
            "delete from review_items where reason = 'collision' "
            "and resolved = 0 and file_id = ?",
            ids,
        )
        conn.executemany(
            "insert into plan_entries(file_id, action, target_relpath, "
            "write_meta, kind, subject) values (?, ?, ?, ?, ?, ?)",
            [
                (e.file_id, e.action, e.target_relpath, int(e.write_meta),
                 e.kind.value if e.kind else None, e.subject)
                for e in entries
            ],
        )
        conn.executemany(
            "insert into review_items(reason, file_id, resolved) "
            "values ('collision', ?, 0)",
            [(f,) for f in collided],
        )
    return entries
