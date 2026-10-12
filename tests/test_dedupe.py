from shelfsort.dedupe import find_exact_duplicates


def test_keeper_prefers_local_then_descriptive(conn, add_file):
    a = add_file("remote", "Woodworking/011167038.pdf", sha="h1")
    b = add_file("remote", "Woodworking/Router Table.pdf", sha="h1")
    c = add_file("local", "x/011167038.pdf", sha="h1")
    assert find_exact_duplicates(conn) == [(c, [a, b])]
    conn.execute("delete from group_members")
    conn.execute("delete from groups")
    conn.execute("delete from files where id=?", (c,))
    assert find_exact_duplicates(conn) == [(b, [a])]


def test_vague_folder_shortest_path_and_id_tiebreak(conn, add_file):
    a = add_file("remote", "Downloads/Book.pdf", sha="h2")
    b = add_file("remote", "Woodworking/Book.pdf", sha="h2")
    assert find_exact_duplicates(conn) == [(b, [a])]
    c = add_file("remote", "A/Same.pdf", sha="h3")
    d = add_file("remote", "B/Same.pdf", sha="h3")
    assert (c, [d]) in find_exact_duplicates(conn)


def test_rerun_replaces_groups_and_records_decisions(conn, add_file):
    a = add_file("local", "a.pdf", sha="h4")
    b = add_file("local", "bb.pdf", sha="h4")
    add_file("local", "ign.txt", sha="h4", status="ignored")
    add_file("local", "solo.pdf", sha="h5")
    find_exact_duplicates(conn)
    assert find_exact_duplicates(conn) == [(a, [b])]
    assert conn.execute("select count(*) from groups where type='exact'").fetchone()[0] == 1
    rows = conn.execute(
        "select file_id, decision from group_members order by file_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(a, "keep"), (b, "quarantine")]
    assert conn.execute("select key from groups").fetchone()[0] == "h4"


def test_missing_quarantined_ignored_rows_never_rank(conn, add_file):
    stale = add_file("remote", "Wood/Book.pdf", sha="h6", status="missing")
    gone = add_file("local", "Book.pdf", sha="h6", status="quarantined")
    real = add_file("remote", "Woodworking/Book.pdf", sha="h6")
    assert find_exact_duplicates(conn) == []
    other = add_file("remote", "Woodworking/Copy/Book.pdf", sha="h6")
    assert find_exact_duplicates(conn) == [(real, [other])]
    assert stale and gone


def test_preferred_source_is_configurable(conn, add_file):
    a = add_file("nas", "x/011167038.pdf", sha="h7")
    b = add_file("local", "Woodworking/Router Table.pdf", sha="h7")
    assert find_exact_duplicates(conn, preferred_source="nas") == [(a, [b])]
    assert find_exact_duplicates(conn) == [(b, [a])]
