from pagination import paginate, total_pages


def test_page_counts():
    assert [total_pages(n, 3) for n in (0, 1, 3, 4, 7)] == [0, 1, 1, 2, 3]


def test_last_partial_page_of_seven():
    assert paginate(list(range(7)), page=3, per_page=3) == [6]
