import pytest
from sqlalchemy import text

from app.retrieval.keyword_search import KeywordSearcher, parse_keyword_query
from app.retrieval.models import RetrievalSource
from app.retrieval.text import (
    identifier_terms,
    lexical_terms,
    query_identifiers,
    query_terms,
    strong_identifiers,
)

FILES = {
    "app/__init__.py": "",
    "app/services/__init__.py": "",
    "app/services/user.py": """
        class UserService:
            def __init__(self, repository):
                self.repository = repository

            def get_user(self, user_id):
                return self.repository.find_by_id(user_id)

            def delete_user(self, user_id):
                self.repository.delete(user_id)
        """,
    "app/stats.py": """
        def average(values):
            # Raises ZeroDivisionError for an empty list.
            return sum(values) / len(values)
        """,
    "app/profile.py": """
        def load_profile_from_database(session, user_id):
            '''Fetch the user profile row from the database.'''
            return session.query(user_id)
        """,
}


# --- term normalization (no database) -----------------------------------------


def test_identifiers_expand_to_whole_and_parts():
    assert identifier_terms("find_by_id") == ["findbyid", "find", "by", "id"]
    assert identifier_terms("UserService") == ["userservice", "user", "service"]
    assert identifier_terms("HTTPError") == ["httperror", "http", "error"]
    assert identifier_terms("user") == ["user"]
    assert lexical_terms("self.repository.find_by_id(x)") == [
        "self", "repository", "findbyid", "find", "by", "id", "x",
    ]


def test_query_terms_drop_stopwords_and_keep_code_words():
    assert query_terms("Where is the error inside get_user()?") == ["error", "getuser", "get", "user"]


def test_query_identifiers_and_strong_identifiers():
    query = "500 error in UserService.get_user and app.database.get_db"
    identifiers = query_identifiers(query)

    assert "userservice.get_user" in identifiers
    assert "get_user" in identifiers
    assert "app.database.get_db" in identifiers
    assert "get_db" in identifiers
    assert {"get_user", "userservice", "get_db"} <= strong_identifiers(query)
    assert "error" not in strong_identifiers(query)


def test_tsquery_contains_only_safe_operands():
    parsed = parse_keyword_query("x') | !(DROP & TABLE:* <-> code_chunks; --")

    for operand in parsed.tsquery.split(" | "):
        assert operand.isalnum()


def test_empty_query_parses_to_nothing():
    assert parse_keyword_query("   ").is_empty
    assert parse_keyword_query("the of and").is_empty


# --- PostgreSQL ------------------------------------------------------------------


@pytest.fixture
def searcher(db_session, index_repo):
    index_repo(FILES, "kw-a")
    return KeywordSearcher(db_session)


def test_exact_identifier_ranks_first(searcher):
    results = searcher.search("kw-a", "500 error when calling get_user()", limit=5)

    assert results[0].qualified_name == "UserService.get_user"
    assert results[0].retrieval_source == RetrievalSource.KEYWORD
    assert results[0].rank == 1
    assert any("exact identifier match 'get_user'" in r for r in results[0].match_reasons)


def test_class_name_matches_the_class_chunk(searcher):
    results = searcher.search("kw-a", "UserService", limit=3)

    assert results[0].qualified_name == "UserService"
    assert results[0].entity_type == "class"


def test_error_message_is_found_by_full_text(searcher):
    results = searcher.search("kw-a", "ZeroDivisionError", limit=3)

    assert results[0].qualified_name == "average"
    assert any(r.startswith("full-text match") for r in results[0].match_reasons)


def test_natural_language_is_found_by_full_text(searcher):
    results = searcher.search("kw-a", "user profile retrieval from database", limit=3)

    assert results[0].qualified_name == "load_profile_from_database"


def test_results_have_source_locations_and_descending_scores(searcher):
    results = searcher.search("kw-a", "user repository", limit=10)

    assert results
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)
    for result in results:
        assert result.file_path.startswith("app/")
        assert 1 <= result.start_line <= result.end_line
        assert result.content


def test_limit_is_applied(searcher):
    assert len(searcher.search("kw-a", "user", limit=2)) == 2
    with pytest.raises(ValueError):
        searcher.search("kw-a", "user", limit=0)


def test_no_results_and_empty_queries(searcher):
    assert searcher.search("kw-a", "kubernetes helm chart", limit=5) == []
    assert searcher.search("kw-a", "", limit=5) == []
    assert searcher.search("kw-a", "   ", limit=5) == []


def test_hostile_query_text_is_harmless(searcher, db_session):
    searcher.search("kw-a", "'); DROP TABLE code_chunks; -- get_user & | ! :*", limit=5)

    assert db_session.execute(text("SELECT count(*) FROM code_chunks")).scalar() > 0


def test_same_function_name_in_two_repositories_stays_isolated(searcher, index_repo):
    index_repo({"other/user.py": "def get_user(user_id):\n    return None\n"}, "kw-b")

    a = searcher.search("kw-a", "get_user", limit=20)
    b = searcher.search("kw-b", "get_user", limit=20)

    assert {r.repository_id for r in a} == {"kw-a"}
    assert {r.repository_id for r in b} == {"kw-b"}
    assert [r.file_path for r in b] == ["other/user.py"]


def test_unknown_repository_returns_nothing(searcher):
    assert searcher.search("no-such-repo", "get_user", limit=5) == []
