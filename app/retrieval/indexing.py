"""Index a repository: read -> Task 9 analysis -> chunks -> embeddings -> one atomic write.

Order matters for consistency:

1. Read and analyze every file once. Each chunk's text and line numbers come
   from the same read, so they cannot drift apart.
2. Chunk with the Task 10 entity IDs.
3. Compute each chunk's ``embedding_hash`` = sha256(model_id + embedding text).
   Reuse every stored vector with the same hash. Embed only the rest, in
   batches. A model change changes every hash, so it re-embeds everything.
4. Replace the index in one transaction (``ChunkRepository.replace_index``).

Steps 1-3 never touch stored chunks. If anything fails, including an
embedding batch halfway through, the attempt is recorded as ``failed`` and the
previous index stays searchable and unchanged.

Nothing here imports or executes repository code. The source revision is read
from ``.git/HEAD`` as a text file; ``git`` is never run.
"""

import hashlib
from pathlib import Path

from pydantic import BaseModel

from app.code_intelligence import analyze_source
from app.code_intelligence.parser import SourceReadError, read_source
from app.code_intelligence.scanner import resolve_repository_root, scan_python_files
from app.knowledge_graph.models import is_valid_repository_id
from app.retrieval.chunker import ChunkerConfig, SourceFile, chunk_repository
from app.retrieval.embeddings import EmbeddingError, EmbeddingProvider, embed_in_batches
from app.retrieval.models import CodeChunk, sha256_text
from app.retrieval.repository import ChunkRepository, StoredChunk

MAX_EMBEDDING_CHARS = 8_000


class IndexingError(Exception):
    """Indexing failed. The previous index, if any, is unchanged. Messages are safe to show."""


class RepositoryIndexConflictError(Exception):
    """The repository ID is already indexed from a different directory."""


class IndexResult(BaseModel):
    repository_id: str
    status: str = "completed"
    index_version: int
    source_revision: str | None
    content_fingerprint: str
    embedding_model: str
    files_indexed: int
    files_failed: int
    chunks_total: int
    chunks_inserted: int
    chunks_updated: int
    chunks_unchanged: int
    chunks_deleted: int
    embeddings_generated: int
    embeddings_reused: int


def embedding_text(chunk: CodeChunk) -> str:
    """What the model sees: a short identity header, then the code."""
    header = [f"{chunk.entity_type} {chunk.qualified_name or chunk.file_path}", f"file {chunk.file_path}"]
    decorators = chunk.metadata.get("decorators") or []
    if decorators:
        header.append("decorators " + " ".join(f"@{d}" for d in decorators))
    return ("\n".join(header) + "\n" + chunk.content)[:MAX_EMBEDDING_CHARS]


def embedding_hash(text: str, model_id: str) -> str:
    return sha256_text(f"{model_id}\0{text}")


def content_fingerprint(chunks: list[CodeChunk]) -> str:
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda c: c.id):
        digest.update(f"{chunk.id}\0{chunk.content_hash}\n".encode())
    return digest.hexdigest()


def read_source_revision(root: Path) -> str | None:
    """The checked-out commit, read from ``.git`` files. None when unavailable."""
    git_dir = root / ".git"
    try:
        head = (git_dir / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:].strip()
        if ".." in ref:
            return None
        ref_file = git_dir / ref
        if ref_file.is_file():
            return ref_file.read_text().strip() or None
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1].strip() == ref:
                    return parts[0]
    except OSError:
        return None
    return None


def load_source_files(root: Path) -> tuple[list[SourceFile], int]:
    """Read and analyze every Python file once. Returns the files and the unreadable count."""
    scan = scan_python_files(root)
    files: list[SourceFile] = []
    unreadable = 0
    for relative_path in scan.files:
        try:
            source = read_source(scan.root / relative_path, display_name=relative_path)
        except SourceReadError:
            unreadable += 1
            continue
        files.append(SourceFile(relative_path, source, analyze_source(source, relative_path)))
    return files, unreadable


class RepositoryIndexer:
    def __init__(
        self,
        repository: ChunkRepository,
        provider: EmbeddingProvider,
        batch_size: int = 64,
        chunker_config: ChunkerConfig | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._batch_size = batch_size
        self._chunker_config = chunker_config

    def index_repository(self, repository_id: str, repository_path: str | Path) -> IndexResult:
        """Index (or reindex) a repository.

        Raises:
            ValueError: invalid repository ID.
            RepositoryNotFoundError / RepositoryNotADirectoryError: bad path.
            RepositoryIndexConflictError: the ID belongs to another directory.
            IndexingError: chunking, embedding or storage failed; nothing changed.
        """
        if not is_valid_repository_id(repository_id):
            raise ValueError(f"Invalid repository id: {repository_id!r}")
        root = resolve_repository_root(repository_path)
        existing = self._repository.get_index_state(repository_id)
        if existing is not None and existing.root_path != str(root):
            raise RepositoryIndexConflictError(
                f"Repository id {repository_id!r} is already indexed from another directory."
            )

        self._repository.begin_attempt(repository_id, str(root))
        try:
            return self._build(repository_id, root)
        except Exception as exc:
            # Database and driver messages can carry SQL and source text; keep only the type.
            safe = str(exc) if isinstance(exc, EmbeddingError | IndexingError) else type(exc).__name__
            self._repository.record_failure(repository_id, str(root), safe)
            raise IndexingError(f"Indexing failed: {safe}") from exc

    def _build(self, repository_id: str, root: Path) -> IndexResult:
        files, unreadable = load_source_files(root)
        chunks = chunk_repository(repository_id, files, self._chunker_config)
        model_id = self._provider.model_id

        texts = [embedding_text(chunk) for chunk in chunks]
        hashes = [embedding_hash(text, model_id) for text in texts]
        vectors = self._repository.existing_embeddings(repository_id, hashes)
        reused = sum(1 for h in hashes if h in vectors)

        # Identical text (e.g. two empty __init__ chunks) is embedded once.
        missing = {h: t for h, t in zip(hashes, texts, strict=True) if h not in vectors}
        new_vectors = embed_in_batches(self._provider, list(missing.values()), self._batch_size)
        vectors.update(zip(missing.keys(), new_vectors, strict=True))

        stored = [
            StoredChunk(chunk, vectors[h], h, model_id) for chunk, h in zip(chunks, hashes, strict=True)
        ]
        failed_files = unreadable + sum(1 for f in files if f.parsed.status != "success")
        revision = read_source_revision(root)
        fingerprint = content_fingerprint(chunks)
        stats = self._repository.replace_index(
            repository_id,
            str(root),
            stored,
            file_count=len(files) + unreadable,
            failed_file_count=failed_files,
            source_revision=revision,
            content_fingerprint=fingerprint,
            embedding_model=model_id,
            embedding_dimensions=self._provider.dimensions,
        )
        return IndexResult(
            repository_id=repository_id,
            index_version=stats.index_version,
            source_revision=revision,
            content_fingerprint=fingerprint,
            embedding_model=model_id,
            files_indexed=len(files),
            files_failed=failed_files,
            chunks_total=len(chunks),
            chunks_inserted=stats.inserted,
            chunks_updated=stats.updated,
            chunks_unchanged=stats.unchanged,
            chunks_deleted=stats.deleted,
            embeddings_generated=len(missing),
            embeddings_reused=reused,
        )
