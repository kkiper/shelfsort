from shelfsort.identify.annas import parse_annas


def test_full_annas_name():
    i = parse_annas("A Beginner's Guide to House Plants_ Creating Beautiful and -- Ryusuke Sakaino "
                    "[境野 隆祐] -- 1, 2022 -- Tuttle Publishing -- 1462923224 -- "
                    "34a156df83fd6ad7a28c0acb1803ad1f -- Anna's Archive.pdf")
    assert i.title.startswith("A Beginner's Guide to House Plants: Creating")
    assert i.authors == ["Ryusuke Sakaino [境野 隆祐]"]
    assert i.year == 2022 and i.publisher == "Tuttle Publishing"
    assert i.isbn == "1462923224" and i.confidence == 0.95
    assert i.method == "annas"


def test_unknown_author_and_no_isbn():
    i = parse_annas("American Woodworker Router Table -- Desconocido -- "
                    "1aa655cf2e3d698418bca2d8523a2531 -- Anna's Archive.epub")
    assert i.authors == [] and i.isbn is None and i.confidence == 0.9


def test_non_annas_returns_none():
    assert parse_annas("All-weather-Morris-Chair3.pdf") is None


def test_requires_separator():
    assert parse_annas("Anna's Archive.pdf") is None


def test_curly_apostrophe_and_hyphenated_isbn():
    i = parse_annas("Title -- Jane Doe -- 978-1-4028-9462-6 -- 1999 -- Pub -- Anna\u2019s Archive.epub")
    assert i.isbn == "9781402894626" and i.year == 1999 and i.publisher == "Pub"
    assert i.authors == ["Jane Doe"]


def test_stem_without_extension_and_first_publisher_only():
    i = parse_annas("T -- Anonymous -- Acme -- Other -- Anna's Archive")
    assert i.authors == [] and i.publisher == "Acme"


def test_invalid_isbn_is_publisher_and_year_range():
    i = parse_annas("T -- A -- 1234567890 -- 1200 -- Anna's Archive.pdf")
    assert i.isbn is None and i.year is None and i.publisher == "1234567890"


def test_space_separated_isbn13():
    i = parse_annas("T -- A -- 978 1 4028 9462 6 -- Anna's Archive.pdf")
    assert i.isbn == "9781402894626" and i.confidence == 0.95 and i.publisher is None


def test_isbn10_lowercase_x_uppercased():
    i = parse_annas("T -- A -- 080442957x -- Anna's Archive.pdf")
    assert i.isbn == "080442957X" and i.publisher is None
