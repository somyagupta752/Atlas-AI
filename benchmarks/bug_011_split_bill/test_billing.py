from billing import split_bill


def test_even_split():
    assert split_bill(1000, 4) == [250, 250, 250, 250]


def test_single_person():
    assert split_bill(999, 1) == [999]


def test_shares_add_up_to_the_total():
    assert sum(split_bill(10000, 3)) == 10000


def test_leftover_cents_go_to_the_first_people():
    assert split_bill(10000, 3) == [3334, 3333, 3333]
    assert split_bill(2, 3) == [1, 1, 0]
