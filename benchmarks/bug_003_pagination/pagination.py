def total_pages(item_count: int, per_page: int) -> int:
    return item_count // per_page


def paginate(items: list, page: int, per_page: int) -> list:
    """Return the items on a 1-based page. Pages beyond the last one are empty."""
    if page < 1 or page > total_pages(len(items), per_page):
        return []
    start = (page - 1) * per_page
    return items[start : start + per_page]
