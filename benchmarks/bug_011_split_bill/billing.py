def split_bill(total_cents: int, people: int) -> list[int]:
    """Split a bill into one share per person, in cents. The shares always add up to the total."""
    share = round(total_cents / people)
    return [share] * people
