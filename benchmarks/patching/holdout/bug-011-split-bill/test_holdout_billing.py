from billing import split_bill


def test_leftover_spread_over_the_first_people():
    assert split_bill(10, 4) == [3, 3, 2, 2]


def test_totals_always_add_up():
    assert all(sum(split_bill(total, people)) == total for total in (0, 1, 7, 99, 1001) for people in (1, 2, 3, 7))
