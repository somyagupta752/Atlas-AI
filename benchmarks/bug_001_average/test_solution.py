from solution import calculate_average


def test_average_of_multiple_numbers():
    assert calculate_average([2, 4, 6]) == 4.0


def test_average_of_single_number():
    assert calculate_average([5]) == 5.0


def test_average_with_negative_numbers():
    assert calculate_average([-2, 2]) == 0.0


def test_average_of_empty_list():
    assert calculate_average([]) == 0.0
