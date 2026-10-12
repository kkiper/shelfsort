from shelfsort import db
from shelfsort import settings as settings_mod


def _saved(client):
    c = db.connect(client.db_path)
    try:
        return settings_mod.load(c)
    finally:
        c.close()


def test_settings_roundtrip(client):
    r = client.post("/settings", data={"magazines": "Wood\nShopNotes", "subject_map": "ww = Woodworking",
                                       "confidence": "0.8", "plan_page_limit": "40"})
    assert r.status_code == 200
    assert "ShopNotes" in client.get("/settings").text
    s = _saved(client)
    assert s.magazines == ["Wood", "ShopNotes"] and s.subject_map == {"ww": "Woodworking"}


def test_settings_all_fields(client):
    client.post("/settings", data={
        "magazines": "A\n\nB\n", "vague_folders": "junk\nstuff",
        "subject_map": "ww = Woodworking\nplants = Gardening",
        "keyword_rules": "Woodworking = chair, table\nGardening = herb",
        "confidence": "0.9", "plan_page_limit": "25", "fuzzy_threshold": "85",
        "ollama_enabled": "on", "ollama_url": "http://o:11434", "ollama_model": "m",
        "sources": "remote = /sources/remote\nother = /x"})
    s = _saved(client)
    assert s.magazines == ["A", "B"] and s.vague_folders == ["junk", "stuff"]
    assert s.subject_map == {"ww": "Woodworking", "plants": "Gardening"}
    assert s.keyword_rules == {"Woodworking": ["chair", "table"], "Gardening": ["herb"]}
    assert (s.confidence, s.plan_page_limit, s.fuzzy_threshold) == (0.9, 25, 85)
    assert s.ollama_enabled and not s.sidecar_all
    assert (s.ollama_url, s.ollama_model) == ("http://o:11434", "m")
    assert s.sources == {"remote": "/sources/remote", "other": "/x"}
    page = client.get("/settings").text
    assert "Woodworking = chair, table" in page and "remote = /sources/remote" in page


def test_settings_invalid_number_is_400_and_not_saved(client):
    client.post("/settings", data={"magazines": "Keep", "confidence": "0.8"})
    r = client.post("/settings", data={"magazines": "Changed", "confidence": "high",
                                       "plan_page_limit": "40"})
    assert r.status_code == 400
    assert "confidence" in r.text and "Changed" in r.text  # error shown, input preserved
    assert _saved(client).magazines == ["Keep"]


def test_settings_malformed_mapping_is_400(client):
    r = client.post("/settings", data={"subject_map": "no equals sign"})
    assert r.status_code == 400 and _saved(client).subject_map == {}


def test_settings_out_of_range_is_400(client):
    assert client.post("/settings", data={"confidence": "1.5"}).status_code == 400
    assert client.post("/settings", data={"plan_page_limit": "0"}).status_code == 400
    assert client.post("/settings", data={"fuzzy_threshold": "101"}).status_code == 400


def test_static_helper_served(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200 and "data-poll" in r.text


import pytest


@pytest.mark.parametrize("url", ["ftp://o:11434", "o:11434", "http://", "not a url", "http://exa mple"])
def test_bad_ollama_url_is_400(client, url):
    r = client.post("/settings", data={"ollama_url": url})
    assert r.status_code == 400 and "ollama_url" in r.text
    assert _saved(client).ollama_url == ""


def test_ollama_enabled_needs_url(client):
    r = client.post("/settings", data={"ollama_enabled": "on", "ollama_url": ""})
    assert r.status_code == 400
    assert client.post("/settings", data={"ollama_url": "https://o.lan:11434"}).status_code == 200


def test_preferred_source_setting(client):
    assert client.post("/settings", data={"preferred_source": "nas"}).status_code == 200
    assert _saved(client).preferred_source == "nas"
    assert 'name="preferred_source"' in client.get("/settings").text
