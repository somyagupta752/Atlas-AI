"""Lexical retrieval: exact identifier matching plus PostgreSQL full-text search.

Score scale (keyword results only; never compared with other strategies):

    score = identifier_score + text_score / (1 + text_score)

- ``identifier_score``: 3.0 when the query names the chunk's qualified or dotted
  name, 2.0 when a code-shaped query word (``get_user``, ``UserService``)
  equals the chunk's symbol name, 1.0 when a plain word does, else 0.
- ``text_score``: ``ts_rank`` over the ``simple`` configuration. Symbol terms
  carry weight A and body terms weight B, so a name match outranks a mention.

The full-text query ORs the query terms, because a bug report rarely uses
every word that appears in the relevant code. Terms are ``[a-z0-9]+`` only
(see ``text.py``), so the tsquery string cannot carry operators from the user.
It is still passed as a bound parameter.
"""

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.models import ChunkType, RetrievalResult, RetrievalSource
from app.retrieval.repository import CHUNK_COLUMNS
from app.retrieval.text import (
    lexical_terms,
    query_identifiers,
    query_terms,
    strong_identifiers,
)

MAX_QUERY_TERMS = 32


@dataclass(frozen=True)
class KeywordQuery:
    raw: str
    terms: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    strong_identifiers: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.terms and not self.identifiers

    @property
    def tsquery(self) -> str:
        return " | ".join(self.terms)


def parse_keyword_query(query: str) -> KeywordQuery:
    identifiers = query_identifiers(query)
    strong = strong_identifiers(query)
    return KeywordQuery(
        raw=query,
        terms=query_terms(query)[:MAX_QUERY_TERMS],
        identifiers=identifiers[:MAX_QUERY_TERMS],
        strong_identifiers=sorted(strong.intersection(identifiers)),
    )


_SEARCH_SQL = f"""
WITH q AS (SELECT CASE WHEN :has_terms THEN to_tsquery('simple', :tsquery) END AS query),
scored AS (
    SELECT {CHUNK_COLUMNS}, symbol_name, dotted_name,
        CASE
            WHEN lower(qualified_name) = ANY(CAST(:strong AS TEXT[]))
              OR lower(dotted_name) = ANY(CAST(:identifiers AS TEXT[])) THEN 3.0
            WHEN lower(symbol_name) = ANY(CAST(:strong AS TEXT[])) THEN 2.0
            WHEN lower(symbol_name) = ANY(CAST(:identifiers AS TEXT[]))
              OR lower(qualified_name) = ANY(CAST(:identifiers AS TEXT[])) THEN 1.0
            ELSE 0.0
        END AS identifier_score,
        CASE WHEN q.query IS NOT NULL AND search_vector @@ q.query
             THEN ts_rank(search_vector, q.query) ELSE 0.0 END AS text_score
    FROM code_chunks, q
    WHERE repository_id = :repository_id
      AND (
            lower(symbol_name) = ANY(CAST(:identifiers AS TEXT[]))
         OR lower(qualified_name) = ANY(CAST(:identifiers AS TEXT[]))
         OR lower(dotted_name) = ANY(CAST(:identifiers AS TEXT[]))
         OR (q.query IS NOT NULL AND search_vector @@ q.query)
      )
)
SELECT *, identifier_score + text_score / (1 + text_score) AS score
FROM scored
ORDER BY score DESC, id
LIMIT :limit
"""


class KeywordSearcher:
    def __init__(self, session: Session) -> None:
        self._session = session

    def search(self, repository_id: str, query: str, limit: int = 20) -> list[RetrievalResult]:
        """Up to ``limit`` chunks from ``repository_id`` only. Empty query: no results."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        parsed = parse_keyword_query(query or "")
        if parsed.is_empty:
            return []
        rows = self._session.execute(
            text(_SEARCH_SQL),
            {
                "repository_id": repository_id,
                "has_terms": bool(parsed.terms),
                "tsquery": parsed.tsquery or "none",
                "identifiers": parsed.identifiers,
                "strong": parsed.strong_identifiers,
                "limit": limit,
            },
        )
        results = []
        for rank, row in enumerate(rows, start=1):
            results.append(
                RetrievalResult(
                    chunk_id=row.id,
                    repository_id=row.repository_id,
                    file_path=row.file_path,
                    content=row.content,
                    start_line=row.start_line,
                    end_line=row.end_line,
                    retrieval_source=RetrievalSource.KEYWORD,
                    score=float(row.score),
                    rank=rank,
                    qualified_name=row.qualified_name,
                    entity_id=row.entity_id,
                    entity_type=ChunkType(row.entity_type),
                    match_reasons=_reasons(parsed, row),
                )
            )
        return results


def _reasons(parsed: KeywordQuery, row: object) -> list[str]:
    reasons = []
    names = {
        "qualified name": (getattr(row, "qualified_name", None) or "").lower(),
        "dotted name": (getattr(row, "dotted_name", None) or "").lower(),
        "name": (getattr(row, "symbol_name", None) or "").lower(),
    }
    for identifier in parsed.identifiers:
        matched = [label for label, value in names.items() if value and value == identifier]
        if matched:
            reasons.append(f"exact identifier match '{identifier}' ({matched[0]})")
            break
    if getattr(row, "text_score", 0) > 0:
        present = set(lexical_terms(f"{row.qualified_name or ''} {row.file_path} {row.content}"))
        matched_terms = [term for term in parsed.terms if term in present][:8]
        if matched_terms:
            reasons.append("full-text match on: " + ", ".join(matched_terms))
    return reasons
