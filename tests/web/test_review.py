import json

import pytest

from shelfsort.models import Identity


def book(title="Chair", authors=("Ann Lee",), conf=0.9):
    return Identity(title=title, authors=list(authors), confidence=conf)


@pytest.fixture
def variant_group(web):
    a = web.add("Woodworking/Chair.pdf", "s1", book())
    b = web.add("Woodworking/Chair.epub", "s2", book())
    web.replan()
    item = web.one("select * from review_items where reason='variant'")
    return type("G", (), {"id": item["id"], "file_ids": [a, b], "pdf": a, "epub": b})


@pytest.fixture
def unidentified_item(web):
    fid = web.add("Woodworking/scan001.pdf", "u1", None, pages=100)
    web.replan()
    item = web.one("select * from review_items where reason='unidentified'")
    return type("I", (), {"id": item["id"], "file_id": fid})


def test_group_decision_requires_a_keeper(web, variant_group):
    r = web.client.post(f"/review/{variant_group.id}/group", follow_redirects=False,
                        data={f"decisions[{f}]": "quarantine" for f in variant_group.file_ids})
    assert r.status_code == 400
    assert web.one("select resolved from review_items where id=?", variant_group.id)[0] == 0


def test_group_quarantine_decision_is_replanned(web, variant_group):
    r = web.client.post(f"/review/{variant_group.id}/group", follow_redirects=False, data={
        f"decisions[{variant_group.pdf}]": "keep",
        f"decisions[{variant_group.epub}]": "quarantine"})
    assert r.status_code == 303
    assert web.one("select resolved from review_items where id=?", variant_group.id)[0] == 1
    assert web.one("select decision from group_members where file_id=?",
                   variant_group.epub)[0] == "quarantine"
    q = web.one("select action, target_relpath from plan_entries where file_id=?",
                variant_group.epub)
    assert (q["action"], q["target_relpath"]) == ("quarantine", "_quarantine/Woodworking/Chair.epub")
    k = web.one("select action, target_relpath from plan_entries where file_id=?",
                variant_group.pdf)
    assert k["target_relpath"] == "Woodworking/Books/Ann Lee/Chair.pdf"
    # a later replan does not bring the resolved group back
    web.replan()
    assert web.one("select count(*) from review_items where resolved=0 and group_id is not null")[0] == 0


def test_single_edit_resolves_and_replans(web, unidentified_item):
    r = web.client.post(f"/review/{unidentified_item.id}/single", follow_redirects=False, data={
        "file_id": unidentified_item.file_id, "magazine": "Wood", "issue_date": "2002-04", "issue_number": "95",
        "cover_story": "Router Jigs", "subject": "Woodworking", "kind": "Magazines"})
    assert r.status_code == 303
    tgt = web.one("select target_relpath from plan_entries where file_id=?",
                  unidentified_item.file_id)[0]
    assert tgt.endswith("Wood - 2002-04 (#95).pdf")
    ident = web.one("select method, confidence from identities where file_id=?",
                    unidentified_item.file_id)
    assert (ident["method"], ident["confidence"]) == ("manual", 1.0)
    assert web.one("select resolved, decision_json from review_items where id=?",
                   unidentified_item.id)[0] == 1


def test_single_subject_override_changes_target(web):
    fid = web.add("misc/x.pdf", "m1", Identity(magazine="Wood", issue_date="2002-04",
                                                 confidence=0.9), pages=60)
    web.replan()
    item = web.one("select * from review_items where file_id=?", fid)
    assert item["reason"] == "subject"
    web.client.post(f"/review/{item['id']}/single", data={
        "file_id": fid, "kind": "Magazines",
        "magazine": "Wood", "issue_date": "2002-04", "subject": "Woodworking"})
    tgt = web.one("select target_relpath from plan_entries where file_id=?", fid)[0]
    assert tgt == "Woodworking/Magazines/Wood/Wood - 2002-04.pdf"


@pytest.mark.parametrize("field,value", [
    ("issue_date", "2002-13"), ("issue_date", "April 2002"),
    ("issue_number", "ninety"), ("kind", "Comics")])
def test_single_bad_input_is_400(web, unidentified_item, field, value):
    data = {"file_id": unidentified_item.file_id, "magazine": "Wood", "kind": "Magazines",
            "subject": "Woodworking", field: value}
    r = web.client.post(f"/review/{unidentified_item.id}/single", data=data)
    assert r.status_code == 400
    assert web.one("select resolved from review_items where id=?", unidentified_item.id)[0] == 0
    assert web.one("select count(*) from identities where method='manual'")[0] == 0


@pytest.fixture
def two_variant_groups_one_without_epub(web):
    web.add("Woodworking/Chair.pdf", "c1", book("Chair"))
    web.add("Woodworking/Chair.epub", "c2", book("Chair"))
    web.add("Woodworking/Table.pdf", "t1", book("Table"))
    web.add("Woodworking/Table.mobi", "t2", book("Table"))
    web.replan()
    assert web.one("select count(*) from review_items where reason='variant'")[0] == 2


def test_bulk_keep_format(web, two_variant_groups_one_without_epub):
    r = web.client.post("/review/bulk", follow_redirects=False,
                        data={"tab": "variant", "action": "keep_format", "format": "epub"})
    assert r.status_code == 303
    assert web.one("select count(*) from review_items where reason='variant' and resolved=0")[0] == 1
    chair_pdf = web.one("select p.action from plan_entries p join files f on f.id=p.file_id"
                        " where f.relpath='Woodworking/Chair.pdf'")[0]
    assert chair_pdf == "quarantine"
    epub = web.one("select p.action from plan_entries p join files f on f.id=p.file_id"
                   " where f.relpath='Woodworking/Chair.epub'")[0]
    assert epub != "quarantine"
    assert web.one("select count(*) from plan_entries p join files f on f.id=p.file_id"
                   " where f.relpath like 'Woodworking/Chair.epub'")[0] == 1  # kept: moves to Books


def test_bulk_accept_suggestions_on_groups_keeps_all(web, two_variant_groups_one_without_epub):
    web.client.post("/review/bulk", data={"tab": "variant", "action": "accept_suggestions"})
    assert web.one("select count(*) from review_items where reason='variant' and resolved=0")[0] == 0
    assert web.one("select count(*) from group_members where decision='quarantine'")[0] == 0


def test_bulk_accept_suggestions_on_singles_skips_unidentified(web):
    ok = web.add("misc/x.pdf", "m1", Identity(magazine="Wood", issue_date="2002-04",
                                                confidence=0.5), pages=60)
    web.add("Woodworking/scan001.pdf", "u1", None, pages=100)
    web.replan()
    # the first has a magazine identity below threshold but no subject: not acceptable;
    # give it a subject folder via a second file that is acceptable
    good = web.add("Woodworking/mag.pdf", "m2", Identity(magazine="Wood", issue_date="2003-01",
                                                          confidence=0.5), pages=60)
    web.replan()
    assert web.one("select reason from review_items where file_id=?", good)[0] == "magazine"
    web.client.post("/review/bulk", data={"tab": "magazine", "action": "accept_suggestions"})
    assert web.one("select resolved from review_items where file_id=?", good)[0] == 1
    assert web.one("select confidence from identities where file_id=? and method='manual'",
                   good)[0] == 1.0
    assert web.one("select resolved from review_items where file_id=?", ok)[0] == 0
    assert web.one("select resolved from review_items where reason='unidentified'")[0] == 0
    assert web.one("select target_relpath from plan_entries where file_id=?", good)[0] == \
        "Woodworking/Magazines/Wood/Wood - 2003-01.pdf"


def test_bulk_rejects_bad_requests(web):
    post = web.client.post
    assert post("/review/bulk", data={"tab": "nope", "action": "accept_suggestions"}).status_code == 400
    assert post("/review/bulk", data={"tab": "variant", "action": "wipe"}).status_code == 400
    assert post("/review/bulk", data={"tab": "variant", "action": "keep_format"}).status_code == 400


def test_collision_accept_resolves_and_stays_resolved(web):
    f = web.add("Woodworking/a.pdf", "a1", book("Chair"))
    web.replan()
    web.conn.execute("insert into review_items(reason, file_id, resolved) values ('collision', ?, 0)", (f,))
    web.conn.commit()
    cid = web.one("select id from review_items where reason='collision'")[0]
    assert web.client.post(f"/review/{cid}/accept", follow_redirects=False).status_code == 303
    assert web.one("select resolved from review_items where id=?", cid)[0] == 1
    assert web.client.post(f"/review/{cid}/accept").status_code == 409


def test_review_pages_render_and_page_size(web):
    for i in range(53):
        web.add(f"Woodworking/scan{i:03}.pdf", f"u{i}", None, pages=100)
    web.replan()
    r = web.client.get("/review?tab=unidentified")
    assert r.status_code == 200 and r.text.count('data-card') == 50
    assert "page=2" in r.text
    assert web.client.get("/review?tab=unidentified&page=2").text.count('data-card') == 3
    assert web.client.get("/review?tab=bogus").status_code == 400
    assert web.client.get("/review/99999").status_code == 404


def test_card_fragment(web, variant_group):
    r = web.client.get(f"/review/{variant_group.id}")
    assert r.status_code == 200 and "<html" not in r.text
    assert f"decisions[{variant_group.pdf}]" in r.text


def test_user_values_are_escaped(web):
    fid = web.add("Woodworking/x.pdf", "x1", None, pages=100)
    web.conn.execute("update files set relpath='Woodworking/<script>alert(1)</script>.pdf'"
                     " where id=?", (fid,))
    web.conn.commit()
    web.replan()
    r = web.client.get("/review?tab=unidentified")
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)" in r.text


def test_review_post_refused_while_job_runs(web, variant_group, slow_job):
    assert web.client.post("/scan").status_code == 202
    r = web.client.post(f"/review/{variant_group.id}/group", data={})
    assert r.status_code == 409


def test_replan_honors_manual_identity(web):
    fid = web.add("Woodworking/scan001.pdf", "u1", None, pages=100)
    web.conn.execute(
        "insert into identities(file_id, method, json, confidence) values (?, 'manual', ?, 1.0)",
        (fid, json.dumps({"title": "Jig Book", "authors": ["Bo"], "isbn": "9780000000002",
                               "method": "manual", "confidence": 1.0})))
    web.conn.commit()
    web.replan()
    assert web.one("select target_relpath from plan_entries where file_id=?", fid)[0] == \
        "Woodworking/Books/Bo/Jig Book.pdf"


@pytest.mark.parametrize("tab", ["near", "possible", "unidentified"])
def test_bulk_keep_format_only_on_variant_tab(web, tab):
    r = web.client.post("/review/bulk", data={"tab": tab, "action": "keep_format", "format": "epub"})
    assert r.status_code == 400


def test_keep_format_button_only_on_variant_tab(web, two_variant_groups_one_without_epub):
    assert "keep_format" in web.client.get("/review?tab=variant").text
    assert "keep_format" not in web.client.get("/review?tab=near").text


def test_keep_in_one_group_beats_quarantine_in_another(web):
    a1 = web.add("Woodworking/Chair.pdf", "s1", book())
    a2 = web.add("Woodworking/Chair2.pdf", "s2", book())
    web.add("Woodworking/Chair.epub", "s3", book())
    web.replan()
    var = web.one("select group_id from review_items where reason='variant'")[0]
    near = web.one("select group_id from review_items where reason='near'")[0]
    assert {r[0] for r in web.conn.execute(
        "select file_id from group_members where group_id=?", (near,))} == {a1, a2}
    web.conn.execute("update group_members set decision='keep' where group_id=? and file_id=?", (var, a1))
    web.conn.execute("update group_members set decision='quarantine' where group_id=? and file_id=?", (near, a1))
    web.conn.execute("update group_members set decision='keep' where group_id=? and file_id=?", (near, a2))
    web.conn.execute("update review_items set resolved=1 where group_id in (?, ?)", (var, near))
    web.conn.commit()
    web.replan()
    act = web.one("select action, target_relpath from plan_entries where file_id=?", a1)
    assert act["action"] != "quarantine" and act["target_relpath"].startswith("Woodworking/Books/")


def test_single_requires_matching_file_id(web, unidentified_item):
    data = {"magazine": "Wood", "kind": "Magazines", "subject": "Woodworking"}
    for extra in ({}, {"file_id": unidentified_item.file_id + 99}):
        r = web.client.post(f"/review/{unidentified_item.id}/single", data={**data, **extra})
        assert r.status_code == 400
    assert web.one("select resolved from review_items where id=?", unidentified_item.id)[0] == 0


@pytest.mark.parametrize("drop", ["kind", "magazine"])
def test_single_requires_kind_and_title_or_magazine(web, unidentified_item, drop):
    data = {"file_id": unidentified_item.file_id, "magazine": "Wood", "kind": "Magazines",
            "subject": "Woodworking"}
    del data[drop]
    assert web.client.post(f"/review/{unidentified_item.id}/single", data=data).status_code == 400
    assert web.one("select count(*) from identities where method='manual'")[0] == 0
    assert web.one("select resolved from review_items where id=?", unidentified_item.id)[0] == 0


def test_single_requires_resolvable_subject(web):
    fid = web.add("misc/x.pdf", "m1", None, pages=100)
    web.replan()
    item = web.one("select id from review_items where file_id=?", fid)[0]
    data = {"file_id": fid, "magazine": "Wood", "kind": "Magazines"}
    assert web.client.post(f"/review/{item}/single", data=data).status_code == 400
    assert web.client.post(f"/review/{item}/single",
                           data={**data, "subject": "Woodworking"}).status_code == 200
