from pagination import paginate


def test_first_page():
    assert paginate([1, 2, 3, 4, 5], page=1, per_page=2) == [1, 2]


def test_full_last_page():
    assert paginate([1, 2, 3, 4], page=2, per_page=2) == [3, 4]


def test_partial_last_page():
    assert paginate([1, 2, 3, 4, 5], page=3, per_page=2) == [5]


def test_page_beyond_end_is_empty():
    assert paginate([1, 2, 3], page=5, per_page=2) == []
