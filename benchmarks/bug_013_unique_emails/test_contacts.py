from contacts import unique_emails


def test_exact_duplicates_removed():
    assert unique_emails(["a@x.com", "b@x.com", "a@x.com"]) == ["a@x.com", "b@x.com"]


def test_first_seen_order():
    assert unique_emails(["c@x.com", "a@x.com", "b@x.com"]) == ["c@x.com", "a@x.com", "b@x.com"]


def test_original_spelling_is_kept():
    assert unique_emails(["Ada.Lovelace@Example.com"]) == ["Ada.Lovelace@Example.com"]


def test_case_variants_are_duplicates():
    assert unique_emails(["Ada@x.com", "ada@X.com", "bob@x.com"]) == ["Ada@x.com", "bob@x.com"]
