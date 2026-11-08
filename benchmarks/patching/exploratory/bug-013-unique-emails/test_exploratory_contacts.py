"""Exploratory: nothing specifies whether surrounding whitespace makes addresses different."""

from contacts import unique_emails


def test_surrounding_whitespace_is_ignored():
    assert unique_emails(["a@x.com", " a@x.com "]) == ["a@x.com"]
