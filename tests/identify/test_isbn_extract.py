from shelfsort.identify.isbn import find_isbns, valid_isbn


def test_find_isbns_variants():
    text = "ISBN 978-1-4629-2322-9 ... also ISBN-10: 1462923224 and junk 1234567890"
    assert find_isbns(text) == ["9781462923229"]


def test_valid_isbn():
    assert valid_isbn("1462923224") and not valid_isbn("1462923225")


def test_valid_isbn_strips_hyphens_spaces_and_checks_13():
    assert valid_isbn("978-1-4629-2322-9")
    assert valid_isbn("978 1 4629 2322 9")
    assert not valid_isbn("9781462923226")
    assert not valid_isbn("12345")
    assert not valid_isbn("")


def test_lone_isbn10_stays_ten_digit_with_x():
    assert find_isbns("ISBN 0-8044-2957-X") == ["080442957X"]
    assert find_isbns("isbn 0-8044-2957-x") == ["080442957X"]


def test_isbn10_after_its_13_is_duplicate_regardless_of_order():
    assert find_isbns("1462923224 then 9781462923229") == ["9781462923229"]


def test_dedupe_and_order_of_appearance():
    text = "9780306406157 and 0306406152 and 9781462923229 and 9780306406157"
    assert find_isbns(text) == ["9780306406157", "9781462923229"]


def test_space_separated_and_no_prefix():
    assert find_isbns("see 978 1 4629 2322 9.") == ["9781462923229"]


def test_does_not_swallow_adjacent_numbers():
    assert find_isbns("2020 9781462923229") == ["9781462923229"]
    assert find_isbns("9781462923229 1462923224 7") == ["9781462923229"]
    assert find_isbns("1462923224 0306406152") == ["1462923224", "0306406152"]


def test_requires_digit_boundaries():
    assert find_isbns("19781462923229") == []
    assert find_isbns("97814629232291") == []


def test_invalid_checksum_and_empty():
    assert find_isbns("ISBN 9781462923226") == []
    assert find_isbns("") == []


def test_hyphenated_13_whose_prefix_is_valid_10_keeps_the_13():
    assert find_isbns("978-0-000003-12-6") == ["9780000003126"]


def test_hyphenated_13_followed_by_unrelated_groups():
    assert find_isbns("978-1-4629-2322-9 2020 55") == ["9781462923229"]
