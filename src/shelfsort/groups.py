from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import PurePosixPath

from rapidfuzz import fuzz

from shelfsort.models import Identity
from shelfsort.names import matching_key

_GROUP_TYPES = ("variant", "near", "possible")
_MARKS = ",".join("?" * len(_GROUP_TYPES))
_MIN_TOKEN = 3


@dataclass
class GroupInput:
    file_id: int
    ext: str
    sha256: str
    subject: str | None
    ident: Identity | None
    filename: str


def _key_for(item: GroupInput) -> str | None:
    ident = item.ident
    if ident is not None and (ident.title or ident.magazine):
        return matching_key(ident)
    stem = PurePosixPath(item.filename.replace("\\", "/")).stem
    return matching_key(Identity(title=re.sub(r"[_-]", " ", stem)))


def _possible_components(
    items: list[GroupInput], keys: dict[int, str], threshold: int
) -> list[list[GroupInput]]:
    """Components of fuzzy-similar book titles within one subject.

    Nodes are (key, subject) pairs, so files sharing a key but filed under a
    different subject do not get dragged into another subject's group.
    """
    nodes: dict[tuple[str, str], list[GroupInput]] = defaultdict(list)
    for m in items:
        key = keys[m.file_id]
        if key.startswith("book|") and m.subject is not None:
            nodes[(key, m.subject)].append(m)
    titles = {n: n[0].split("|")[1] for n in nodes}
    tokens = {
        n: [t for t in dict.fromkeys(v.split()) if len(t) >= _MIN_TOKEN]
        for n, v in titles.items()
    }
    first = {
        n: (v.split()[0] if v.split() and len(v.split()[0]) >= _MIN_TOKEN else None)
        for n, v in titles.items()
    }
    index: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for n, toks in tokens.items():
        for t in toks:
            index[(n[1], t)].append(n)
    shared: dict[tuple, int] = defaultdict(int)
    for ns in index.values():
        for a, b in combinations(sorted(ns), 2):
            shared[(a, b)] += 1
    # Same first token but no other shared token: still a candidate pair.
    by_first: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for n, f in first.items():
        if f is not None:
            by_first[(n[1], f)].append(n)
    for ns in by_first.values():
        for a, b in combinations(sorted(ns), 2):
            shared.setdefault((a, b), 0)

    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), n in shared.items():
        if a[0] == b[0] or a[1] != b[1]:
            continue
        if n < 2 and (first[a] is None or first[a] != first[b]):
            continue
        if fuzz.token_set_ratio(titles[a], titles[b]) >= threshold:
            parent[find(a)] = find(b)
    comps: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for n in nodes:
        comps[find(n)].append(n)
    return [
        [m for n in c for m in nodes[n]] for c in comps.values() if len(c) > 1
    ]


def build_groups(
    conn: sqlite3.Connection, items: list[GroupInput], fuzzy_threshold: int
) -> None:
    try:
        # Rebuild: drop unresolved variant/near/possible groups (children first).
        old = [
            r[0]
            for r in conn.execute(
                f"select id from groups where type in ({_MARKS}) and id in "
                "(select group_id from review_items where resolved=0)", _GROUP_TYPES)
        ]
        for table, col in (("review_items", "group_id"), ("group_members", "group_id"),
                           ("groups", "id")):
            conn.executemany(f"delete from {table} where {col}=?", [(g,) for g in old])
        # Groups still present were resolved by the user: leave their files alone.
        decided = {
            r[0]
            for r in conn.execute(
                "select gm.file_id from group_members gm join groups g "
                f"on g.id=gm.group_id where g.type in ({_MARKS})", _GROUP_TYPES)
        }

        by_key: dict[str, list[GroupInput]] = defaultdict(list)
        for item in items:
            if item.file_id in decided:
                continue
            key = _key_for(item)
            if key is not None:
                by_key[key].append(item)

        def add(gtype: str, key: str, members: list[GroupInput]) -> None:
            gid = conn.execute(
                "insert into groups(type, key) values (?, ?)", (gtype, key)).lastrowid
            conn.executemany(
                "insert into group_members(group_id, file_id, decision) "
                "values (?, ?, null)",
                [(gid, fid) for fid in sorted({m.file_id for m in members})])
            conn.execute(
                "insert into review_items(reason, file_id, group_id, resolved) "
                "values (?, null, ?, 0)", (gtype, gid))

        for key in sorted(by_key):
            members = by_key[key]
            by_ext: dict[str, list[GroupInput]] = defaultdict(list)
            for m in members:
                by_ext[m.ext].append(m)
            if len(by_ext) > 1:
                add("variant", key, members)
            for ext in sorted(by_ext):
                if len({m.sha256 for m in by_ext[ext]}) > 1:
                    add("near", f"{key}|{ext}", by_ext[ext])

        keys = {m.file_id: k for k, ms in by_key.items() for m in ms}
        all_items = [m for ms in by_key.values() for m in ms]
        for members in _possible_components(all_items, keys, fuzzy_threshold):
            ids = sorted({m.file_id for m in members})
            add("possible", "possible|" + ",".join(map(str, ids)), members)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
