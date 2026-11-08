import re


def slugify(title: str) -> str:
    """Lowercase ASCII words joined by '-': 'Hello, World!' -> 'hello-world'."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return "-".join(words)
