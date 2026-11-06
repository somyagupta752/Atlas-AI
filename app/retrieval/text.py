"""Identifier-aware text normalization shared by indexing and search.

PostgreSQL's default parser splits ``find_by_id`` into ``find``, ``by``, ``id``
and treats ``app.database.get`` as a host name. Code needs both the whole
identifier and its parts, so terms are produced here, in Python, and the
database only stores and matches them with the ``simple`` configuration.

``lexical_terms("UserService.find_by_id")`` gives::

    ["userservice", "user", "service", "findbyid", "find", "by", "id"]

A compound identifier is kept as one term with its underscores removed
(``findbyid``), because the parser would split it again at an underscore.
Terms contain only ``[a-z0-9]``, so they are always safe tsquery operands.

Light suffix folding replaces ``lists`` with ``list`` and ``runs`` with
``run``, identically on the index and the query side. Terms are replaced, not
added, so a suffixed word is never counted twice in ``ts_rank``. Exact
identifier matching compares symbol names and does not use these terms.

``LEXICAL_VERSION`` is part of every stored row's hash. Bump it whenever term
generation changes, so a reindex rewrites rows whose source did not change.
"""

import re

LEXICAL_VERSION = 3

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+")

# Words that carry no meaning in a code search. Short on purpose: identifiers
# such as "get", "list" or "error" are meaningful and must stay.
STOPWORDS = frozenset(
    {
        "a", "about", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by", "can",
        "could", "did", "do", "does", "doing", "for", "from", "had", "has", "have", "having", "how",
        "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our", "so",
        "some", "such", "than", "that", "the", "their", "them", "then", "there", "these", "they",
        "this", "those", "through", "to", "too", "up", "us", "was", "we", "were", "what", "when",
        "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you", "your",
        "happens", "happening", "getting", "occurs", "occurring", "inside", "code", "function",
        "functions",
    }
)


def fold_suffix(term: str) -> str:
    """Crude suffix folding: ``loaded`` -> ``load``, ``lists`` -> ``list``."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(term) > len(suffix) + 3 and term.endswith(suffix):
            return term[: -len(suffix)]
    return term


def _with_folded(terms: list[str]) -> list[str]:
    return [fold_suffix(term) for term in terms]


def split_identifier(identifier: str) -> list[str]:
    """``UserService`` -> ``["user", "service"]``; ``get_db`` -> ``["get", "db"]``."""
    parts: list[str] = []
    for piece in identifier.split("_"):
        parts.extend(match.lower() for match in _CAMEL_PART.findall(piece))
    return [part for part in parts if part]


def identifier_terms(identifier: str) -> list[str]:
    """The collapsed identifier first, then its parts. Lower-case ``[a-z0-9]`` only."""
    collapsed = identifier.replace("_", "").lower()
    parts = split_identifier(identifier)
    terms = [collapsed] if collapsed else []
    if len(parts) > 1 or (parts and parts[0] != collapsed):
        terms.extend(parts)
    return [term for term in terms if term and not term.isdigit()]


def lexical_terms(text: str) -> list[str]:
    """Every identifier in ``text`` expanded into terms, in order, duplicates kept."""
    terms: list[str] = []
    for match in _IDENTIFIER.finditer(text):
        terms.extend(_with_folded(identifier_terms(match.group())))
    return terms


def identifier_terms_of(text: str) -> list[str]:
    """Like ``lexical_terms`` but without suffix folding."""
    terms: list[str] = []
    for match in _IDENTIFIER.finditer(text):
        terms.extend(identifier_terms(match.group()))
    return terms


def query_terms(text: str) -> list[str]:
    """Distinct search terms for a query, stopwords removed, first occurrence order."""
    seen: dict[str, None] = {}
    for match in _IDENTIFIER.finditer(text):
        word = match.group()
        if word.lower() in STOPWORDS:
            continue
        for term in _with_folded(identifier_terms(word)):
            if term not in STOPWORDS:
                seen.setdefault(term, None)
    return list(seen)


def looks_like_identifier(word: str) -> bool:
    """``get_user``, ``UserService``, ``getUser``, ``app.db``: written like code, not prose."""
    if "_" in word.strip("_") or "." in word:
        return True
    return bool(re.search(r"[a-z][A-Z]|[A-Z][a-z]+[A-Z]", word))


def query_identifiers(text: str) -> list[str]:
    """Candidate symbol names in a query, lower-cased, for exact name matching.

    Dotted names are kept whole and also contribute their last segment, so
    ``app.database.get_db`` matches the chunk ``get_db`` in module ``app.database``.
    """
    seen: dict[str, None] = {}
    for match in _DOTTED.finditer(text):
        dotted = match.group().strip(".")
        seen.setdefault(dotted.lower(), None)
        seen.setdefault(dotted.rsplit(".", 1)[-1].lower(), None)
    for match in _IDENTIFIER.finditer(text):
        word = match.group()
        if word.lower() not in STOPWORDS and len(word) > 1:
            seen.setdefault(word.lower(), None)
    return list(seen)


def strong_identifiers(text: str) -> set[str]:
    """The subset of ``query_identifiers`` written like code. They earn a larger boost."""
    strong = {m.group().lower() for m in _DOTTED.finditer(text)}
    strong |= {m.group().rsplit(".", 1)[-1].lower() for m in _DOTTED.finditer(text)}
    strong |= {
        m.group().lower() for m in _IDENTIFIER.finditer(text) if looks_like_identifier(m.group())
    }
    return strong
