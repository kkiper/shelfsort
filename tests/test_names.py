from shelfsort.models import Identity
from shelfsort.names import matching_key, normalize_title, sanitize_component


def test_sanitize_strips_illegal_and_trailing():
    assert sanitize_component("459331368<.pdf") == "459331368.pdf"
    assert sanitize_component("Title. ") == "Title"


def test_sanitize_keeps_cjk_and_falls_back():
    assert "境野 隆祐" in sanitize_component("Ryusuke Sakaino [境野 隆祐]")
    assert sanitize_component("<<<") == "untitled"
    assert sanitize_component("<<<.pdf") == "untitled.pdf"


def test_sanitize_truncates_keeping_ext():
    out = sanitize_component("x" * 300 + ".epub")
    assert len(out) == 200 and out.endswith(".epub")


def test_sanitize_collapses_whitespace_and_control_chars():
    assert sanitize_component("a \t  b\x00c") == "a bc"


def test_normalize_and_key():
    assert normalize_title("The Router Table, 2nd Edition!") == "router table 2nd"
    a = Identity(title="American Woodworker Router Table", authors=[])
    b = Identity(title="american woodworker router table")
    assert matching_key(a) == matching_key(b)
    m = Identity(magazine="Wood", issue_date="2002-04")
    assert matching_key(m) == "mag|wood|2002-04"


def test_normalize_folds_latin_keeps_cjk_and_splits_hyphens():
    assert normalize_title("Café Déjà-Vu") == "cafe deja vu"
    assert normalize_title("境野 隆祐") == "境野 隆祐"
    assert normalize_title("Router-Table") == "router table"


def test_normalize_drops_anna_tail_and_whole_word_noise():
    assert normalize_title("Some Book -- Anna's Archive") == "some book"
    assert normalize_title("Theory of Volumes") == "theory of volumes"


def test_matching_key_book_with_author_and_missing_parts():
    i = Identity(title="Great Book", authors=["José Álvarez", "X"])
    assert matching_key(i) == "book|great book|jose alvarez"
    assert matching_key(Identity()) is None
    assert matching_key(Identity(magazine="Wood")) is None


def test_normalize_leaves_kana_and_hangul_unchanged():
    assert normalize_title("が") == "が"
    assert normalize_title("한글") == "한글"
    assert normalize_title("Ａｂ") == "ab"


def test_matching_key_none_when_normalized_empty():
    assert matching_key(Identity(title="The")) is None
    assert matching_key(Identity(title="!!!")) is None
    assert matching_key(Identity(magazine="The", issue_date="2002-04")) is None


def test_sanitize_strips_path_separators():
    assert sanitize_component("AC/DC") == "ACDC"
    assert sanitize_component("a\\b") == "ab"
