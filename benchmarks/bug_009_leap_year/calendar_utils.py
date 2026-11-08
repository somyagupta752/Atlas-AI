def is_leap_year(year: int) -> bool:
    """Gregorian rules: every 4th year, except centuries, except every 400th year."""
    return year % 4 == 0 and year % 100 != 0


def days_in_february(year: int) -> int:
    return 29 if is_leap_year(year) else 28
