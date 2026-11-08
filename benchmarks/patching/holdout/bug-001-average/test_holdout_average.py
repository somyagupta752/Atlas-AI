from solution import calculate_average


def test_tuple_input():
    assert calculate_average((1, 2, 3)) == 2.0


def test_empty_tuple():
    assert calculate_average(()) == 0.0
