"""Exploratory: behaviour the specification does not define.

parse_scores' docstring defines only well-formed input and blank input. What a
trailing comma ("a:1,") should do is unspecified: ignoring the empty item and
raising an error are both defensible. This test is informational. Its result is
reported, but it never counts toward the held-out pass rate or success.
"""

from parsing import parse_scores


def test_trailing_comma():
    assert parse_scores("a:1,") == {"a": 1}
