from calendar_utils import days_in_february, is_leap_year


def test_common_year():
    assert not is_leap_year(2023)


def test_every_fourth_year():
    assert is_leap_year(2024)


def test_century_is_not_leap():
    assert not is_leap_year(1900)


def test_every_400th_year_is_leap():
    assert is_leap_year(2000)


def test_february_2000():
    assert days_in_february(2000) == 29
