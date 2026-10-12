from shelfsort.classify import classify_kind, classify_subject
from shelfsort.models import Identity, Kind


def test_subject_from_folder_map_and_fallback(settings):
    settings.subject_map = {"ww": "Woodworking"}
    assert classify_subject("WW/a.pdf", None, "a.pdf", settings) == "Woodworking"
    assert classify_subject("gardening/a.pdf", None, "a.pdf", settings) == "Gardening"


def test_subject_keeps_mixed_case_folder(settings):
    assert classify_subject("WW Projects/a.pdf", None, "a.pdf", settings) == "WW Projects"


def test_subject_map_keys_case_insensitive(settings):
    settings.subject_map = {"WW": "Woodworking"}
    assert classify_subject("ww/a.pdf", None, "a.pdf", settings) == "Woodworking"


def test_subject_vague_uses_keywords(settings):
    settings.keyword_rules = {"Woodworking": ["router", "workbench", "morris chair"]}
    assert classify_subject("Downloads/x.pdf", None, "Garage-Workbench-Plans.pdf", settings) == "Woodworking"
    assert classify_subject("misc/x.pdf", None, "x.pdf", settings) is None


def test_subject_root_file_uses_keywords_and_title(settings):
    settings.keyword_rules = {"Woodworking": ["morris chair"]}
    ident = Identity(title="Building the Morris Chair")
    assert classify_subject("x.pdf", ident, "x.pdf", settings) == "Woodworking"


def test_keyword_plural_whole_word_and_order(settings):
    settings.keyword_rules = {"A": ["router"], "B": ["table"]}
    assert classify_subject("Downloads/x.pdf", None, "Routers.pdf", settings) == "A"
    assert classify_subject("Downloads/x.pdf", None, "subrouter.pdf", settings) is None
    assert classify_subject("Downloads/x.pdf", None, "router table.pdf", settings) == "A"


def test_kind_rules(settings):
    assert classify_kind(Identity(isbn="9781462923229"), "pdf", 300, settings) == Kind.BOOK
    assert classify_kind(Identity(magazine="Wood"), "pdf", 90, settings) == Kind.MAGAZINE
    assert classify_kind(None, "pdf", 12, settings) == Kind.PLAN
    assert classify_kind(None, "jpg", None, settings) == Kind.PLAN
    assert classify_kind(None, "pdf", 41, settings) is None


def test_kind_book_by_method_and_authors(settings):
    ident = Identity(method="annas", authors=["A"])
    assert classify_kind(ident, "epub", None, settings) == Kind.BOOK
    assert classify_kind(Identity(method="annas"), "epub", None, settings) is None
    assert classify_kind(Identity(method="ollama", authors=["A"]), "pdf", 90, settings) is None
