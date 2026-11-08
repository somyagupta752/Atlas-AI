from calendar_utils import is_leap_year


def test_other_400th_years():
    assert is_leap_year(1600) and is_leap_year(2400)


def test_other_centuries():
    assert not is_leap_year(2100) and not is_leap_year(1800)
