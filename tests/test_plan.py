from shelfsort.models import Identity, Kind
from shelfsort.plan import PlanInput, build_plan, target_relpath


def pi(file_id, name, source="remote", relpath=None, **kw):
    return PlanInput(
        file_id=file_id,
        source=source,
        relpath=relpath or f"Woodworking/{name}.pdf",
        ext="pdf",
        kind=kw.pop("kind", Kind.PLAN),
        subject="Woodworking",
        ident=kw.pop("ident", None),
        quarantine=kw.pop("quarantine", False),
    )


def test_target_paths():
    assert target_relpath(Kind.MAGAZINE, "Woodworking", Identity(magazine="American Woodworker", issue_date="2002-04", issue_number=95), "x.pdf", "pdf") \
        == "Woodworking/Magazines/American Woodworker/American Woodworker - 2002-04 (#95).pdf"
    assert target_relpath(Kind.PLAN, "Woodworking", None, "arch-top-bookends<.pdf", "pdf") \
        == "Woodworking/Plans & Articles/Arch Top Bookends.pdf"
    assert target_relpath(Kind.BOOK, "Gardening", Identity(title="House Plants", authors=["R. Sakaino"], year=2022), "x.pdf", "pdf") \
        == "Gardening/Books/R. Sakaino/House Plants (2022).pdf"


def test_target_paths_fallbacks():
    assert target_relpath(Kind.BOOK, "G", Identity(title="T"), "x.pdf", "PDF") == "G/Books/Unknown Author/T.pdf"
    assert target_relpath(Kind.BOOK, "G", None, "my_book.PDF", "pdf") == "G/Books/Unknown Author/My Book.pdf"
    assert target_relpath(Kind.MAGAZINE, "G", Identity(magazine="Mag"), "x.pdf", "pdf") == "G/Magazines/Mag/Mag - undated.pdf"
    assert target_relpath(Kind.PLAN, "G", None, "Keep_Case.pdf", "pdf") == "G/Plans & Articles/Keep Case.pdf"
    assert target_relpath(Kind.BOOK, "G", Identity(title="A/B", authors=["X\\Y"]), "x.pdf", "pdf") == "G/Books/XY/AB.pdf"


def test_collision_within_plan_and_case_insensitive(conn, add_file, tmp_source):
    tmp_source.write("Woodworking/Plans & Articles/morris chair.pdf")
    a = add_file("remote", "Woodworking/Morris Chair.pdf")
    b = add_file("remote", "Woodworking/Morris_Chair.pdf")
    entries = build_plan(conn, [pi(a, "Morris Chair"), pi(b, "Morris_Chair")], tmp_source.sources)
    assert [e.target_relpath.rsplit("/", 1)[1] for e in entries] == ["Morris Chair (2).pdf", "Morris Chair (3).pdf"]
    assert conn.execute("select count(*) from review_items where reason='collision' and resolved=0").fetchone()[0] == 2
    assert conn.execute("select count(*) from plan_entries").fetchone()[0] == 2


def test_in_place_rename_and_freed_slot(conn, add_file, tmp_source):
    d = "Woodworking/Plans & Articles"
    tmp_source.write(f"{d}/Chair.pdf")
    tmp_source.write(f"{d}/old.pdf")
    tmp_source.write("Woodworking/old.pdf")
    inplace = add_file("remote", f"{d}/Chair.pdf")
    renamed = add_file("remote", f"{d}/old.pdf")
    moved = add_file("remote", "Woodworking/old.pdf")
    entries = build_plan(conn, [
        pi(inplace, "Chair", relpath=f"{d}/Chair.pdf"),
        pi(renamed, "x", relpath=f"{d}/old.pdf", ident=Identity(title="Bench")),
        pi(moved, "old", relpath="Woodworking/old.pdf"),
    ], tmp_source.sources)
    by = {e.file_id: e for e in entries}
    assert inplace not in by
    assert by[renamed].action == "rename" and by[renamed].write_meta
    assert by[renamed].target_relpath == f"{d}/Bench.pdf"
    # old.pdf's slot was freed by the rename, so the move takes it unsuffixed.
    assert by[moved].action == "move" and by[moved].target_relpath == f"{d}/Old.pdf"
    assert conn.execute("select count(*) from review_items").fetchone()[0] == 0


def test_quarantine_collision_no_review_and_replacement(conn, add_file, tmp_source):
    tmp_source.write("_quarantine/a/x.pdf")
    f = add_file("remote", "a/x.pdf")
    g = add_file("remote", "b/x.pdf")
    e = build_plan(conn, [pi(f, "x", relpath="a/x.pdf", quarantine=True)], tmp_source.sources)
    assert e[0].action == "quarantine" and e[0].target_relpath == "_quarantine/a/x (2).pdf"
    assert not e[0].write_meta
    assert conn.execute("select count(*) from review_items").fetchone()[0] == 0
    conn.execute("insert into review_items(reason, file_id, resolved) values ('collision', ?, 0)", (f,))
    conn.execute("insert into review_items(reason, file_id, resolved) values ('collision', ?, 1)", (f,))
    conn.commit()
    build_plan(conn, [pi(f, "x", relpath="a/x.pdf", quarantine=True), pi(g, "y", relpath="b/x.pdf")], tmp_source.sources)
    assert conn.execute("select count(*) from plan_entries").fetchone()[0] == 2
    assert conn.execute("select count(*) from review_items where resolved=0").fetchone()[0] == 0
    assert conn.execute("select count(*) from review_items where resolved=1").fetchone()[0] == 1


def test_collisions_are_per_source(conn, add_file, tmp_source):
    a = add_file("remote", "Woodworking/Z.pdf")
    b = add_file("other", "Woodworking/Z.pdf")
    entries = build_plan(
        conn,
        [pi(a, "Z", source="remote"), pi(b, "Z", source="other")],
        {**tmp_source.sources, "other": str(tmp_source.root / "nope")},
    )
    assert [e.target_relpath for e in entries] == ["Woodworking/Plans & Articles/Z.pdf"] * 2


def test_rolls_back_on_error(conn, add_file, tmp_source):
    import pytest
    import sqlite3

    f = add_file("remote", "Woodworking/Q.pdf")
    build_plan(conn, [pi(f, "Q")], tmp_source.sources)
    with pytest.raises(sqlite3.IntegrityError):  # unknown file id (foreign key)
        build_plan(conn, [pi(f, "Q2"), pi(9999, "Nope")], tmp_source.sources)
    rows = conn.execute("select file_id, target_relpath from plan_entries").fetchall()
    assert [tuple(r) for r in rows] == [(f, "Woodworking/Plans & Articles/Q.pdf")]


def test_already_at_suffixed_name_is_in_place(conn, add_file, tmp_source):
    d = "Woodworking/Plans & Articles"
    tmp_source.write(f"{d}/Foo.pdf")
    tmp_source.write(f"{d}/Foo (2).pdf")
    f = add_file("remote", f"{d}/Foo (2).pdf")
    entries = build_plan(conn, [pi(f, "Foo", relpath=f"{d}/Foo (2).pdf")], tmp_source.sources)
    assert entries == []
    assert conn.execute("select count(*) from plan_entries").fetchone()[0] == 0
    assert conn.execute("select count(*) from review_items").fetchone()[0] == 0


def test_unknown_source_raises(conn, add_file, tmp_source):
    import pytest

    f = add_file("nowhere", "Woodworking/Z.pdf")
    with pytest.raises(KeyError):
        build_plan(conn, [pi(f, "Z", source="nowhere")], tmp_source.sources)
