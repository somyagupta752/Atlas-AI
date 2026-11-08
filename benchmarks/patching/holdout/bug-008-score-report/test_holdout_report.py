from report import summarize


def test_whitespace_only():
    assert summarize("   ") == {"count": 0, "mean": 0.0, "best": None}
