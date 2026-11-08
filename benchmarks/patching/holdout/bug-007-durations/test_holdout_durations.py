from durations import to_seconds


def test_upper_case_seconds():
    assert to_seconds("45S") == 45


def test_seconds_everywhere():
    assert [to_seconds(v) for v in ("1s", "59s", "2h", "3m")] == [1, 59, 7200, 180]
