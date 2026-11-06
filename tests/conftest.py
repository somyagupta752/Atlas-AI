from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from app.database import engine


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A session whose changes are rolled back after each test.

    Commits inside the test only release a savepoint, so the real tables stay clean.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def write_repo(tmp_path):
    """Write ``{relative_path: source}`` into a fresh directory and return its path."""
    import textwrap
    from pathlib import Path

    def write(files: dict[str, str], name: str = "repo") -> Path:
        root = tmp_path / name
        root.mkdir()
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source))
        return root

    return write


@pytest.fixture
def make_graph(write_repo):
    """Analyze ``{relative_path: source}`` and build its code graph."""
    from app.code_intelligence import analyze_repository
    from app.knowledge_graph.builder import build_code_graph

    def make(files: dict[str, str], repository_id: str = "demo", name: str = "repo"):
        return build_code_graph(analyze_repository(write_repo(files, name)), repository_id)

    return make


@pytest.fixture
def chunk_repo(db_session):
    """A ChunkRepository on the rollback-wrapped session."""
    from app.retrieval.repository import ChunkRepository

    return ChunkRepository(db_session)


@pytest.fixture
def index_repo(write_repo, chunk_repo):
    """Write files, then index them. Returns ``(IndexResult, root)``.

    The default provider is the local hashing provider: deterministic, no network.
    """
    from app.retrieval.embeddings import HashingEmbeddingProvider
    from app.retrieval.indexing import RepositoryIndexer

    def index(files, repository_id="demo", provider=None, name=None, batch_size=64):
        root = write_repo(files, name or repository_id)
        indexer = RepositoryIndexer(
            chunk_repo, provider or HashingEmbeddingProvider(), batch_size=batch_size
        )
        return indexer.index_repository(repository_id, root), root

    return index
