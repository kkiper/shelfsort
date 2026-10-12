import pytest

from shelfsort.groups import GroupInput, build_groups
from shelfsort.models import Identity


@pytest.fixture
def files(conn, add_file):
    """Ensure file rows 1..8 exist (group_members has a foreign key)."""
    for i in range(1, 9):
        add_file("src", f"f{i}.pdf")


def gi(fid, ext, sha, title, subj="Woodworking"):
    return GroupInput(fid, ext, sha, subj, Identity(title=title), f"{title}.{ext}")


def types(conn):
    return sorted(r["type"] for r in conn.execute("select type from groups"))


def test_unidentified_files_group_by_filename(conn, files):
    build_groups(conn, [GroupInput(1, "pdf", "a", "Woodworking", None, "Morris_Chair.pdf"),
                        GroupInput(2, "pdf", "b", "Woodworking", None, "Morris-Chair.pdf")], 90)
    assert types(conn) == ["near"]


def test_variant_and_near(conn, files):
    build_groups(conn, [gi(1, "epub", "a", "Router Table"), gi(2, "lit", "b", "router table"),
                        gi(3, "pdf", "c", "Morris Chair"), gi(4, "pdf", "d", "Morris Chair")], 90)
    assert types(conn) == ["near", "variant"]
    assert conn.execute("select count(*) from review_items where resolved=0 "
                        "and group_id is not null and file_id is null").fetchone()[0] == 2
    assert conn.execute("select count(*) from group_members where decision is not null"
                        ).fetchone()[0] == 0


def test_possible_match_same_subject_only(conn, files):
    build_groups(conn, [gi(1, "pdf", "a", "Weekend Woodworking Projects"),
                        gi(2, "pdf", "b", "American Woodworker Weekend Woodworking Projects"),
                        gi(3, "pdf", "c", "Weekend Woodworking Projects", subj="Gardening")], 90)
    members = [r["file_id"] for r in conn.execute(
        "select file_id from group_members join groups g on g.id=group_id "
        "where g.type='possible' order by file_id")]
    assert members == [1, 2]


def test_possible_chain_is_one_group(conn, files):
    build_groups(conn, [gi(1, "pdf", "a", "Weekend Woodworking Projects"),
                        gi(2, "pdf", "b", "Weekend Woodworking Projects Volume Basics"),
                        gi(3, "pdf", "c", "Weekend Woodworking Projects Volume Basics Extra")], 90)
    assert types(conn) == ["possible"]
    assert conn.execute("select count(*) from group_members").fetchone()[0] == 3


def test_same_magazine_different_issues_no_groups(conn, files):
    def mag(fid, issue):
        return GroupInput(fid, "pdf", f"s{fid}", "Woodworking",
                          Identity(magazine="Wood", issue_date=issue), "wood.pdf")
    build_groups(conn, [mag(1, "2002-04"), mag(2, "2002-05")], 90)
    assert types(conn) == []


def test_rerun_is_idempotent(conn, files):
    items = [gi(1, "epub", "a", "Router Table"), gi(2, "lit", "b", "router table"),
             gi(3, "pdf", "c", "Morris Chair"), gi(4, "pdf", "d", "Morris Chair")]
    build_groups(conn, items, 90)
    first = conn.execute("select count(*) from groups").fetchone()[0]
    build_groups(conn, items, 90)
    assert conn.execute("select count(*) from groups").fetchone()[0] == first == 2
    assert conn.execute("select count(*) from review_items").fetchone()[0] == 2


def test_resolved_groups_kept_and_members_excluded(conn, files):
    items = [gi(3, "pdf", "c", "Morris Chair"), gi(4, "pdf", "d", "Morris Chair")]
    build_groups(conn, items, 90)
    conn.execute("update review_items set resolved=1")
    conn.commit()
    build_groups(conn, items + [gi(5, "pdf", "e", "Router Table"),
                                gi(6, "epub", "f", "Router Table")], 90)
    assert types(conn) == ["near", "variant"]
    assert conn.execute("select count(*) from review_items where resolved=1").fetchone()[0] == 1


def test_exact_groups_untouched(conn, files):
    conn.execute("insert into groups(type, key) values ('exact', 'sha')")
    conn.commit()
    build_groups(conn, [], 90)
    assert types(conn) == ["exact"]


# ------------------------------------------------------------ I4: scale

def test_possible_groups_scale_and_stay_small(conn):
    import random
    import string
    import time
    from collections import Counter

    from shelfsort.groups import GroupInput, build_groups
    from shelfsort.models import Identity

    rng = random.Random(7)
    vocab = ["".join(rng.choices(string.ascii_lowercase, k=6)) for _ in range(400)]
    items = []
    for i in range(3000):
        fid = conn.execute(
            "insert into files(source, relpath, size, mtime, sha256, ext, status)"
            " values ('r', ?, 1, 0, ?, 'pdf', 'new')", (f"W/{i}.pdf", f"s{i}")).lastrowid
        title = f"woodworking projects volume {rng.choice(vocab)} {rng.choice(vocab)}"
        items.append(GroupInput(fid, "pdf", f"s{i}", "Woodworking",
                                Identity(title=title, authors=["Ann"], confidence=0.9),
                                f"{i}.pdf"))
    conn.commit()
    t = time.perf_counter()
    build_groups(conn, items, 90)
    elapsed = time.perf_counter() - t
    sizes = Counter(r[0] for r in conn.execute(
        "select gm.group_id from group_members gm join groups g on g.id = gm.group_id"
        " where g.type = 'possible'"))
    assert elapsed < 10, elapsed
    assert sizes and max(sizes.values()) <= 20
