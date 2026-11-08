def parse_scores(text: str) -> dict[str, int]:
    """'alice:3, bob:5' -> {'alice': 3, 'bob': 5}. Blank input means no scores."""
    scores = {}
    for item in text.split(","):
        name, value = item.split(":")
        scores[name.strip()] = int(value)
    return scores
