from parsing import parse_scores
from report import summarize


def test_parse_scores():
    assert parse_scores("alice:3, bob:5") == {"alice": 3, "bob": 5}


def test_parse_blank():
    assert parse_scores("") == {}


def test_summary():
    assert summarize("alice:3, bob:5") == {"count": 2, "mean": 4.0, "best": "bob"}


def test_summary_of_blank_input():
    assert summarize("") == {"count": 0, "mean": 0.0, "best": None}
