from parsing import parse_scores


def summarize(text: str) -> dict:
    """Count, mean and best player. No scores: count 0, mean 0.0, best None."""
    scores = parse_scores(text)
    values = list(scores.values())
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 2),
        "best": max(scores, key=scores.get),
    }
