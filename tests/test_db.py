from shelfsort import db, models


def test_connect_creates_all_tables(tmp_path):
    conn = db.connect(str(tmp_path / "s.db"))
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"files", "identities", "groups", "group_members", "review_items",
            "plan_entries", "batches", "journal", "lookup_cache", "settings"} <= names


def test_connect_uses_wal_and_row_factory(conn):
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    conn.execute("insert into settings(key, json) values ('k', '{}')")
    assert conn.execute("select key from settings").fetchone()["key"] == "k"


def test_identity_defaults_not_shared():
    a, b = models.Identity(), models.Identity()
    a.authors.append("x")
    assert b.authors == [] and a.confidence == 0.0 and a.method == ""


def test_enum_values():
    assert models.Kind.PLAN.value == "Plans & Articles"
    assert models.FileStatus.QUARANTINED == "quarantined"
