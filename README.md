Atlas-AI

## Code intelligence

`app/code_intelligence/` analyzes Python repositories with the standard-library `ast` module. It never imports or executes the analyzed code.

| Module | Responsibility |
| --- | --- |
| `scanner.py` | Find `.py` files under one root. Skips `.git`, `.venv`, `__pycache__`, build output and symlinks that leave the root. |
| `parser.py` | Read files (PEP 263 encodings, 5 MB limit) and parse them. Read errors and syntax errors are separate exceptions. |
| `extractor.py` | One `ast.NodeVisitor` pass: classes, functions, methods, parameters, imports, calls, inheritance. |
| `analyzer.py` | Orchestrate a repository. A failing file becomes a structured error and does not stop the others. |
| `schemas.py` | Pydantic response models. |

`functions` lists every `def`, including methods. Use `kind` (`function`, `method`, `nested_function`) and `parent_class` to tell them apart. Calls are syntactic: `service.calculate()` is recorded as written, not resolved to a class.

```bash
curl -X POST localhost:8000/repositories/analyze \
  -H 'Content-Type: application/json' \
  -d '{"repository_path": "/absolute/path/to/repo"}'
```

Responses: 200 with the analysis, 404 if the path does not exist, 400 if it is not a directory, 403 if it is outside `ALLOWED_REPOSITORY_ROOTS`, 422 for a missing or relative path.

The endpoint fails closed. Set `ALLOWED_REPOSITORY_ROOTS` to a JSON list of directories, for example `ALLOWED_REPOSITORY_ROOTS=["/Users/me/code"]` in `.env`. If it is unset or empty, every request returns 403. Requested paths and allowed roots are both resolved before the containment check, so symlinks and `..` cannot escape a root. The allowlist check runs before the existence check, so a rejected caller cannot learn which paths exist.


## Code knowledge graph

`app/knowledge_graph/` turns a code-intelligence analysis into a graph and stores it in Neo4j. The builder, resolver and validator do not depend on Neo4j.

| Module | Responsibility |
| --- | --- |
| `models.py` | Pydantic nodes, relationships, unresolved references and the ID scheme. |
| `resolver.py` | Resolve imports, calls and base classes from evidence. It never matches by name alone. |
| `builder.py` | `build_code_graph(analysis)`: a deterministic graph, sorted by ID. |
| `validator.py` | `validate_code_graph(graph)`: structured errors. It never drops anything. |
| `repository.py` | `GraphRepository` protocol and `InMemoryGraphRepository`. |
| `neo4j_client.py`, `neo4j_repository.py` | Driver lifecycle, and transactional batched persistence. |
| `queries.py` | All Cypher, parameterized. |
| `service.py` | Build orchestration used by the API. |

Graph shape: `Repository -CONTAINS-> File -CONTAINS-> Class | Function`, `Class -HAS_METHOD-> Function`, `File -IMPORTS-> File`, `Function -CALLS-> Function`, `Class -EXTENDS-> Class`. Every node also has the `CodeEntity` label, which carries one uniqueness constraint on `id`.

IDs embed the repository and the file path, for example `function:atlas-ai:app/services/user.py::UserService.find_user`. Two files that both define `UserService` therefore never collide.

A reference without enough evidence becomes an `UnresolvedReference` with a reason, such as `external_module`, `unknown_type`, `ambiguous` or `builtin`. It is never turned into a guessed relationship. Each `CALLS` relationship has a `resolution_status`. The value `resolved` means the target follows from definitions and imports. The value `inferred` means it relies on type evidence, such as `self.repo = Repository()`.

### Run Neo4j

1. Start the container: `docker compose up -d neo4j`. It listens on bolt port 7688 and browser port 7475, so it does not clash with other Neo4j instances.
2. Copy the `NEO4J_*` lines from `.env.example` into `.env`.
3. Start the API. Startup creates the constraints and indexes.

If Neo4j is not configured or not reachable, the graph endpoints return 503. The rest of the API keeps working.

### Endpoints

| Method | Path | Result |
| --- | --- | --- |
| POST | `/repositories/graph/build` | Analyze, build, validate and persist. The body is `{"repository_path": ..., "repository_id": optional}`. |
| GET | `/repositories/{repository_id}/graph` | The full graph, including unresolved references. |
| GET | `/repositories/{repository_id}/graph/stats` | Counts by label, by relationship type and by unresolved reason. |
| DELETE | `/repositories/{repository_id}/graph` | 204, or 404 if there is no graph. |

The build endpoint uses the same `ALLOWED_REPOSITORY_ROOTS` check as `/repositories/analyze`. The repository ID defaults to the lower-cased directory name. If that ID already belongs to a different directory, the build returns 409.

A rebuild replaces the graph in one transaction. If the save fails, the previous graph stays unchanged.

### Tests

Run `uv run pytest`. The tests in `tests/test_neo4j_integration.py` need the Neo4j container. They skip when it is unavailable. To fail instead of skipping, as in CI, run `REQUIRE_NEO4J=1 uv run pytest`.


## Code retrieval

`app/retrieval/` finds the code that is relevant to a bug report or a question. It combines three strategies and returns ranked evidence with source locations. It does not diagnose anything: relevance is not proof of cause. Nothing in this path calls an LLM or runs repository code.

| Module | Responsibility |
| --- | --- |
| `models.py` | `CodeChunk`, `RetrievalResult`, `RetrievedEvidence`, `RetrievalResponse`. |
| `chunker.py` | AST-aware chunks: classes, functions, methods, nested functions, module code. |
| `text.py` | Identifier-aware terms (`find_by_id` gives `findbyid find by id`). |
| `embeddings.py` | `EmbeddingProvider` protocol, local hashing provider, HTTP provider, test fake. |
| `repository.py` | All SQL for `code_chunks` and `repository_indexes`. Atomic index replacement. |
| `keyword_search.py` | Exact identifier match plus PostgreSQL full-text search. |
| `vector_search.py` | pgvector cosine similarity. |
| `graph_search.py` | Neo4j neighborhood expansion with weighted, directed paths. |
| `fusion.py` | Reciprocal Rank Fusion (k = 60, equal weights). |
| `ranker.py` | Overlap folding and evidence packaging. |
| `indexing.py` | `RepositoryIndexer.index_repository`: incremental, all-or-nothing. |
| `service.py` | `RetrievalService.retrieve_code`: the hybrid pipeline. |
| `evaluation.py` | Precision@K, Recall@K, MRR. |

### Set up

1. Start PostgreSQL with pgvector: `docker compose up -d postgres`. The image is `pgvector/pgvector:0.8.0-pg16`.
2. Apply migrations: `uv run alembic upgrade head`. This enables the `vector` extension and creates the tables.
3. Optional: build the graph with `POST /repositories/graph/build` and the same repository ID. Without it, search uses keyword and vector only.

### Chunks

A chunk is one class, function, method, nested function or run of module-level statements. Its line range is exact, and its `entity_id` is the Task 10 graph node ID. Class and method chunks overlap on purpose, because the class keeps context that the method lacks. A function longer than 150 lines or 12,000 characters becomes consecutive `#partN` windows. A larger class becomes a header chunk. A file with a syntax error stays searchable as line windows.

Chunk IDs derive from entity IDs, for example `chunk:function:atlas-ai:app/user.py::UserService.get_user`. They are stable for unchanged source and unique across repositories.

### Embeddings

The default provider, `hashing`, runs locally. It hashes identifier sub-terms into 1024 dimensions. It relates `get_user` to "user", but it does not know that "fetch" and "load" are similar. For a real model, set `EMBEDDING_PROVIDER=openai_compatible` and `EMBEDDING_BASE_URL`, for example a local Ollama.

If the endpoint is not on localhost, it receives your repository's source code. Such an endpoint is refused unless `EMBEDDING_ALLOW_EXTERNAL=true`. An API key is never sent over plain HTTP to another host. Error messages never include the key or the response body.

The `embedding` column is `VECTOR(1024)`. `EMBEDDING_DIMENSIONS` must match it, or startup disables the provider. A model with another dimension needs a new migration. Each vector is stored with its `model_id`, and search compares only vectors with the same `model_id`.

### Indexing

Incremental indexing works by content hash. Each chunk's `embedding_hash` is the SHA-256 of the model ID plus the embedded text. A reindex reuses every stored vector with the same hash. Only new or changed chunks are embedded. A model change changes every hash, so everything is embedded again.

Failure handling is all or nothing:

1. Parsing, chunking and embedding run first. They do not touch stored data.
2. One transaction then upserts changed rows, deletes stale ones and publishes `index_version + 1`.

If any step fails, including an embedding batch halfway through, the attempt is marked `failed` and the previous index stays searchable.

### Retrieval

1. Keyword search. An exact name match scores 1 to 3. Full-text search uses `ts_rank` over the `simple` configuration, with names weighted above bodies.
2. Vector search. The score is `similarity = 1 - cosine_distance`, in [-1, 1].
3. Graph search. Seeds are the entities named in the query plus the top 5 keyword and vector hits. Paths of up to 2 hops are followed in both directions and never pass through the Repository node.
   - Each step is weighted by type and direction. A caller weighs 1.0, a containing file 0.5.
   - An `inferred` step costs a factor of 0.7.
   - "Who calls X" favours callers, and "what does X call" favours callees.
4. Reciprocal Rank Fusion merges the lists by rank, not by raw score, because the three score scales are not comparable.
5. Overlap folding. A chunk inside a higher-ranked chunk of the same file is folded into that chunk's `merged` list, with its own reason.

If Neo4j or the embedding provider fails, that strategy is reported in `strategies` and the others still answer.

### Endpoints

| Method | Path | Result |
| --- | --- | --- |
| POST | `/repositories/{repository_id}/index` | Index or reindex. The body is `{"repository_path": ...}`. |
| GET | `/repositories/{repository_id}/index/status` | State, version, model, counts and last error. |
| DELETE | `/repositories/{repository_id}/index` | 204, or 404 if there is no index. |
| POST | `/repositories/{repository_id}/search` | The body is `{"query": ..., "limit": 1-50}`. |

Indexing uses the same `ALLOWED_REPOSITORY_ROOTS` check as the other repository endpoints. Index errors: 403 outside the allowlist, 404 for a missing path, 409 if the ID belongs to another directory, 503 if the embedding provider fails. Search errors: 422 for an empty query or an invalid limit, 404 if the repository is not indexed. Responses never contain embeddings.

### Evaluation

`benchmarks/retrieval/cases.json` holds 19 cases about this repository. Expected results are graph entity IDs. To measure:

```bash
uv run python benchmarks/retrieval/evaluate.py            # graph built in memory
uv run python benchmarks/retrieval/evaluate.py --graph neo4j --output results.json
```

The script indexes the repository under the ID `atlas-ai`. It refuses to run if an expected entity no longer exists. Results with the hashing provider, on the repository as of this change:

| Strategy | P@1 | R@5 | R@10 | MRR |
| --- | --- | --- | --- | --- |
| keyword | 0.37 | 0.61 | 0.69 | 0.523 |
| vector | 0.37 | 0.62 | 0.72 | 0.516 |
| graph | 0.16 | 0.39 | 0.39 | 0.281 |
| hybrid | 0.32 | 0.72 | 0.85 | 0.558 |

Hybrid has the best recall and MRR. Its P@1 is lower than keyword or vector alone. If keyword and vector both rank a chunk first but graph search does not list it, that chunk can drop below graph neighbors that collect three RRF contributions. The cases `error-not-allowed-root` and `parse-without-executing` show this.
