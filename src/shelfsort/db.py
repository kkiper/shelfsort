import sqlite3

SCHEMA = """
create table if not exists files (
    id integer primary key,
    source text not null,
    relpath text not null,
    size integer not null,
    mtime real not null,
    sha256 text,
    ext text not null,
    pages integer,
    status text not null default 'new',
    unique (source, relpath)
);
create table if not exists identities (
    file_id integer primary key references files(id),
    method text not null,
    json text not null,
    confidence real not null
);
create table if not exists groups (
    id integer primary key,
    type text not null,
    key text not null
);
create table if not exists group_members (
    group_id integer not null references groups(id),
    file_id integer not null references files(id),
    decision text,
    primary key (group_id, file_id)
);
create table if not exists review_items (
    id integer primary key,
    reason text not null,
    file_id integer references files(id),
    group_id integer references groups(id),
    resolved integer not null default 0,
    decision_json text
);
create table if not exists plan_entries (
    file_id integer primary key references files(id),
    action text not null,
    target_relpath text not null,
    write_meta integer not null default 0,
    kind text,
    subject text
);
create table if not exists batches (
    id integer primary key,
    scope text not null,
    state text not null,
    created text not null
);
create table if not exists journal (
    id integer primary key,
    batch_id integer not null references batches(id),
    seq integer not null,
    op text not null,
    src text,
    dst text,
    state text not null
);
create table if not exists lookup_cache (
    key text primary key,
    json text not null,
    fetched text not null
);
create table if not exists settings (
    key text primary key,
    json text not null
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
