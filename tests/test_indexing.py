import textwrap

import pytest
from sqlalchemy import text

from app.code_intelligence import RepositoryNotFoundError
from app.retrieval.embeddings import FakeEmbeddingProvider
from app.retrieval.indexing import (
    IndexingError,
    RepositoryIndexConflictError,
    RepositoryIndexer,
    embedding_hash,
    embedding_text,
    read_source_revision,
)
from app.retrieval.models import ChunkType, CodeChunk
from app.retrieval.repository import INDEX_STATUS_FAILED, INDEX_STATUS_READY

V1 = {
    "app/__init__.py": "",
    "app/user.py": """
        class UserService:
            def get_user(self, user_id):
                return user_id

            def delete_user(self, user_id):
                return None
        """,
    "app/stats.py": "def average(values):\n    return sum(values) / len(values)\n",
}


def rows(db_session, repository_id):
    return {
        row.id: row
        for row in db_session.execute(
            text(
                "SELECT id, content_hash, embedding_hash, index_version, start_line "
                "FROM code_chunks WHERE repository_id = :r"
            ),
            {"r": repository_id},
        )
    }


def rewrite(root, relative, source):
    (root / relative).write_text(textwrap.dedent(source))


def test_first_index_stores_every_chunk_with_hash_and_embedding(index_repo, chunk_repo, db_session):
    provider = FakeEmbeddingProvider()
    result, root = index_repo(V1, "idx-a", provider=provider)

    stored = rows(db_session, "idx-a")
    assert result.index_version == 1
    assert result.chunks_total == len(stored) == result.chunks_inserted
    assert result.embeddings_generated == result.chunks_total
    assert result.embeddings_reused == 0
    assert "chunk:function:idx-a:app/user.py::UserService.get_user" in stored
    null_embeddings = db_session.execute(
        text("SELECT count(*) FROM code_chunks WHERE repository_id = 'idx-a' AND embedding IS NULL")
    ).scalar()
    assert null_embeddings == 0

    state = chunk_repo.get_index_state("idx-a")
    assert state.status == INDEX_STATUS_READY
    assert state.is_searchable
    assert state.chunk_count == result.chunks_total
    assert state.embedding_model == provider.model_id
    assert state.root_path == str(root.resolve())


def test_unchanged_reindex_reuses_every_embedding_and_writes_nothing(index_repo, chunk_repo, db_session):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    before = rows(db_session, "idx-a")

    provider = FakeEmbeddingProvider()
    result = RepositoryIndexer(chunk_repo, provider).index_repository("idx-a", root)

    assert provider.document_calls == []  # no embedding call at all
    assert result.embeddings_generated == 0
    assert result.embeddings_reused == result.chunks_total
    assert result.chunks_unchanged == result.chunks_total
    assert (result.chunks_inserted, result.chunks_updated, result.chunks_deleted) == (0, 0, 0)
    assert result.index_version == 2
    after = rows(db_session, "idx-a")
    assert set(after) == set(before)
    assert all(after[i].index_version == 1 for i in after)  # rows were not rewritten


def test_modified_function_is_the_only_one_re_embedded(index_repo, chunk_repo, db_session):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    before = rows(db_session, "idx-a")
    rewrite(root, "app/stats.py", "def average(values):\n    return sum(values) / max(len(values), 1)\n")

    provider = FakeEmbeddingProvider()
    result = RepositoryIndexer(chunk_repo, provider).index_repository("idx-a", root)

    average = "chunk:function:idx-a:app/stats.py::average"
    assert result.embeddings_generated == 1
    assert len(provider.embedded_texts) == 1
    assert "max(len(values), 1)" in provider.embedded_texts[0]
    after = rows(db_session, "idx-a")
    assert after[average].content_hash != before[average].content_hash
    assert result.chunks_updated == 1
    unchanged = set(before) - {average}
    assert all(after[i].embedding_hash == before[i].embedding_hash for i in unchanged)


def test_removed_chunks_are_deleted_and_new_ones_added(index_repo, chunk_repo, db_session):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    rewrite(
        root,
        "app/user.py",
        """
        class UserService:
            def get_user(self, user_id):
                return user_id

            def rename_user(self, user_id, name):
                return name
        """,
    )

    result = RepositoryIndexer(chunk_repo, FakeEmbeddingProvider()).index_repository("idx-a", root)

    after = rows(db_session, "idx-a")
    assert "chunk:function:idx-a:app/user.py::UserService.delete_user" not in after
    assert "chunk:function:idx-a:app/user.py::UserService.rename_user" in after
    assert result.chunks_deleted == 1
    assert result.chunks_inserted == 1


def test_reindexing_never_creates_duplicates(index_repo, chunk_repo, db_session):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    for _ in range(3):
        RepositoryIndexer(chunk_repo, FakeEmbeddingProvider()).index_repository("idx-a", root)

    count = db_session.execute(
        text("SELECT count(*), count(DISTINCT id) FROM code_chunks WHERE repository_id = 'idx-a'")
    ).one()
    assert count[0] == count[1] == chunk_repo.get_index_state("idx-a").chunk_count


def test_moved_lines_update_locations_without_re_embedding(index_repo, chunk_repo, db_session):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    stats = (root / "app/stats.py").read_text()
    (root / "app/stats.py").write_text("\n\n\n" + stats)

    provider = FakeEmbeddingProvider()
    result = RepositoryIndexer(chunk_repo, provider).index_repository("idx-a", root)

    assert result.embeddings_generated == 0
    assert rows(db_session, "idx-a")["chunk:function:idx-a:app/stats.py::average"].start_line == 4


def test_embedding_model_change_re_embeds_everything(index_repo, chunk_repo):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider(model="v1"))

    provider = FakeEmbeddingProvider(model="v2")
    result = RepositoryIndexer(chunk_repo, provider).index_repository("idx-a", root)

    assert result.embeddings_generated == result.chunks_total
    assert chunk_repo.get_index_state("idx-a").embedding_model == provider.model_id


def test_provider_failure_halfway_keeps_the_previous_index(index_repo, chunk_repo, db_session):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    before = rows(db_session, "idx-a")
    rewrite(root, "app/stats.py", "def median(values):\n    return sorted(values)[0]\n")
    rewrite(root, "app/user.py", "def changed():\n    return 1\n")

    failing = FakeEmbeddingProvider(fail_on_call=2)
    with pytest.raises(IndexingError, match="Fake provider failure"):
        RepositoryIndexer(chunk_repo, failing, batch_size=1).index_repository("idx-a", root)

    assert len(failing.document_calls) == 2  # it really failed mid-way
    assert rows(db_session, "idx-a").keys() == before.keys()
    state = chunk_repo.get_index_state("idx-a")
    assert state.status == INDEX_STATUS_FAILED
    assert state.index_version == 1
    assert state.is_searchable
    assert "Fake provider failure" in state.last_error


def test_failed_first_index_is_not_searchable(index_repo, chunk_repo):
    with pytest.raises(IndexingError):
        index_repo(V1, "idx-new", provider=FakeEmbeddingProvider(fail_on_call=1))

    state = chunk_repo.get_index_state("idx-new")
    assert state.status == INDEX_STATUS_FAILED
    assert not state.is_searchable
    assert chunk_repo.count_chunks("idx-new") == 0


def test_storage_failure_rolls_back_and_hides_internal_details(index_repo, chunk_repo, monkeypatch):
    _, root = index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    rewrite(root, "app/stats.py", "def total(values):\n    return sum(values)\n")
    before = chunk_repo.count_chunks("idx-a")

    def boom(*args, **kwargs):
        raise RuntimeError("SELECT secret FROM somewhere -- internal detail")

    monkeypatch.setattr(chunk_repo, "replace_index", boom)
    with pytest.raises(IndexingError) as info:
        RepositoryIndexer(chunk_repo, FakeEmbeddingProvider()).index_repository("idx-a", root)

    assert "internal detail" not in str(info.value)
    assert chunk_repo.get_index_state("idx-a").last_error == "RuntimeError"
    assert chunk_repo.count_chunks("idx-a") == before


def test_repository_id_bound_to_another_directory_is_a_conflict(index_repo, write_repo, chunk_repo):
    index_repo(V1, "idx-a", provider=FakeEmbeddingProvider())
    other = write_repo({"x.py": "def x():\n    pass\n"}, "elsewhere")

    with pytest.raises(RepositoryIndexConflictError):
        RepositoryIndexer(chunk_repo, FakeEmbeddingProvider()).index_repository("idx-a", other)


def test_missing_path_and_invalid_id(chunk_repo, tmp_path):
    indexer = RepositoryIndexer(chunk_repo, FakeEmbeddingProvider())
    with pytest.raises(RepositoryNotFoundError):
        indexer.index_repository("idx-a", tmp_path / "missing")
    with pytest.raises(ValueError):
        indexer.index_repository("Not Valid!", tmp_path)


def test_repository_with_no_python_code_indexes_as_empty(index_repo, chunk_repo):
    result, _ = index_repo({"README.md": "hello", "empty.py": ""}, "idx-empty")

    assert result.chunks_total == 0
    assert chunk_repo.get_index_state("idx-empty").is_searchable


def test_identical_text_is_embedded_once(index_repo):
    provider = FakeEmbeddingProvider()
    files = {"a.py": "def same():\n    return 1\n"}
    index_repo(files, "idx-dup", provider=provider)

    assert len(provider.embedded_texts) == len(set(provider.embedded_texts))


def test_embedding_hash_depends_on_text_and_model():
    chunk = CodeChunk(
        id="chunk:function:demo:a.py::f",
        repository_id="demo",
        file_path="a.py",
        entity_type=ChunkType.FUNCTION,
        qualified_name="f",
        content="def f(): pass",
        start_line=1,
        end_line=1,
    )
    body = embedding_text(chunk)

    assert body.startswith("function f\nfile a.py\n")
    assert embedding_hash(body, "m1") != embedding_hash(body, "m2")
    assert embedding_hash(body, "m1") == embedding_hash(body, "m1")


def test_source_revision_is_read_without_running_git(tmp_path):
    git = tmp_path / ".git"
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs/heads/main").write_text("abc123\n")
    assert read_source_revision(tmp_path) == "abc123"

    (git / "refs/heads/main").unlink()
    (git / "packed-refs").write_text("# pack-refs\ndef456 refs/heads/main\n")
    assert read_source_revision(tmp_path) == "def456"

    assert read_source_revision(tmp_path / "nope") is None
