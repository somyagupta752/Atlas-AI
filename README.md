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

### Known limitations

These limit how far the results can be trusted. They are not defects in the design.

- The benchmark numbers come from hashing embeddings. They test the retrieval pipeline, not a semantic embedding model. A real model can change the ranking a lot, so measure again before comparing strategies.
- 19 cases about one repository work as a regression check. They do not show general retrieval quality. Add deliberately hard queries and other repositories before drawing conclusions.
- Graph candidate selection does not tell production code from test code. In the `build-graph-dependencies` case, test functions that call `GraphService.build_graph` crowd out its production callees. Fix this in a later ranking change, not with a special case.


## Debugging agent

`app/agents/` investigates a reported bug. It calls read-only tools, records what they return, keeps competing hypotheses and returns a root-cause analysis that is checked against the evidence it collected. It never modifies files. The loop is our own code: no agent framework.

| Module | Responsibility |
| --- | --- |
| `llm.py` | `LLMProvider` protocol, `LLMResponse`, the Anthropic adapter and `FakeLLM`. SDK objects never leave this module. |
| `state.py` | `InvestigationState` and `Observation`. |
| `schemas.py` | Limits, statuses, `Hypothesis`, `Evidence`, `DiagnosisDraft`, `RootCauseAnalysis`. |
| `tool_registry.py` | Registration, duplicate rejection, schemas, argument validation. |
| `tool_executor.py` | One untrusted tool call to one bounded `Observation`. |
| `loop.py` | The agent loop and every stopping condition. |
| `prompts.py` | The system prompt and the fixed follow-up messages. |
| `hypotheses.py`, `evidence.py`, `diagnosis.py` | Validate the final answer against the observations. |
| `investigator.py` | Loop, then diagnosis, repair and status. |
| `agent.py` | `DebuggingAgent.investigate_bug(repository_id, bug_report)`. No HTTP. |
| `repository.py` | PostgreSQL persistence of investigations and events. |
| `routes.py` | The `/debug` endpoints. |
| `evaluation.py` | Benchmark scoring. |

### How a tool call moves

1. The loop sends the conversation and the permitted tool schemas to the provider.
2. The adapter turns the provider's response into an `LLMResponse` with `ToolCall`s.
3. The loop appends the assistant turn, then executes each call in order.
4. The executor checks the tool is permitted, validates the arguments, runs the tool with a time limit and bounds the output.
5. The result becomes an `Observation` with an ID such as `obs-3`, and a tool message that answers the call ID.
6. The adapter groups the tool messages into one `tool_result` turn, and the loop calls the model again.
7. A response without tool calls is the final answer: a JSON diagnosis.

### Tools

| Tool | Reuses | Notes |
| --- | --- | --- |
| `retrieve_evidence` | Task 11 hybrid retrieval | Ranked chunks with provenance. Relevance is not proof. |
| `search_code` | | Literal text search with file and line. |
| `read_file` | | Numbered lines, at most 400 per call. |
| `get_symbol` | Task 9 AST analysis | Definition, kind, range, parent class, parameters, source. |
| `get_callers`, `get_callees` | Task 10 graph | Neo4j when it holds the repository, otherwise the same graph built in memory. Callees include unresolved calls. |
| `run_tests` | Task 3 runner | A test path, never a command. |

Tool security:

- Paths must be relative. `..`, absolute paths and symlinks that resolve outside the root are refused.
- Files that may hold secrets (`.env`, keys, `.netrc` and similar) are refused. Credentials in returned text are redacted.
- `run_tests` accepts only `test_*.py` or `*_test.py`, plus an optional test name checked against a strict pattern. The command is fixed: `python -m pytest -q -p no:cacheprovider <path>`. It runs with a minimal environment (no API keys, no database URL), no bytecode writes and a hard timeout.
- `run_tests` is refused unless the repository is under `AGENT_TRUSTED_TEST_ROOTS`.

Running tests is still code execution. The runner is not a sandbox. Use trusted repositories only, until isolated workers exist.

### Limits

| Setting | Default | On reaching it |
| --- | --- | --- |
| `AGENT_MAX_ITERATIONS` | 12 | The last iteration is a final turn: tools are forbidden and the model must answer. |
| `AGENT_MAX_TOOL_CALLS` | 20 | Extra calls are skipped, then a final turn. |
| `AGENT_TOOL_TIMEOUT_SECONDS` | 15 | The call becomes a `timeout` observation. |
| `AGENT_MAX_TOOL_OUTPUT_BYTES` | 20000 | Output is truncated. Evidence may cite only what the model saw. |
| `AGENT_MAX_DURATION_SECONDS` | 120 | The loop stops with `limit_reached`. |

A repeated identical call is answered from the earlier observation and is not run again. After more than 3 repeats, 3 responses in a row with only rejected or repeated calls, or 2 empty responses, the model gets a final turn. An answer from a final turn is reported as `limit_reached`, never as `completed`.

### Diagnosis validation

The final answer must be one JSON object (`DiagnosisDraft`). The application then checks:

- Each evidence item cites observation IDs that exist and succeeded, from tools that match its type. For example, `execution` evidence must cite `run_tests`.
- File paths and line ranges lie inside what those observations showed. A qualified name appears in them.
- A `supported` hypothesis has supporting evidence. A `rejected` one has contradicting evidence.
- `completed` needs a root cause and a supported hypothesis backed by source-code, test or execution evidence. `inconclusive` must have no root cause.
- `affected_files` appear in the evidence. Confidence is a number from 0 to 1.

`tests_run` is built from `run_tests` observations, never from the model's text. `fix_applied` is always `false`. An invalid answer gets one repair turn with the errors listed. If it is still invalid, the status is `failed` (or `limit_reached` after a limit), with no root cause.

### Statuses

| Status | Meaning |
| --- | --- |
| `running` | Stored before the loop starts. |
| `completed` | A validated, evidence-backed diagnosis. |
| `inconclusive` | The evidence was not sufficient. No root cause. |
| `limit_reached` | A limit stopped the investigation. The evidence collected so far is kept. |
| `failed` | LLM error, refusal, invalid diagnosis or internal error. |
| `cancelled` | Stopped through a `CancellationToken`. |

Terminal statuses cannot change.

### Prompt injection

Tool results and the bug report are data. The system prompt says so, and the bug report sits inside `<bug_report>` tags that it cannot close. The real defence is in code: the model can only call registered, permitted tools; every argument is validated; and no tool can write, run a command or reach the network.

### LLM provider

The adapter uses the official `anthropic` SDK, model `claude-opus-5-5` at effort `high`.

- The key comes from `ANTHROPIC_API_KEY`. It is never logged or returned.
- The agent sends repository code to Anthropic, so it is refused unless `LLM_ALLOW_EXTERNAL=true`.
- Timeouts are explicit. The SDK retries 429, 5xx and connection errors with backoff and honours `retry-after`. After that, errors become `LLMTimeoutError`, `LLMRateLimitError`, `LLMProviderError` or `LLMConfigurationError`. Their messages contain the status and request ID only.
- Prompt caching is on, because the system prompt and tool list are identical on every turn.
- Server-side fallbacks (`fallbacks: "default"`) are on: a request declined by a safety classifier is retried on a recommended model. Turn them off with `LLM_SERVER_FALLBACKS=false`.

Another provider needs only a new class with the same `generate` method.

### Endpoints

| Method | Path | Result |
| --- | --- | --- |
| POST | `/debug/investigate` | Body `{"repository_id": ..., "bug_report": ...}`. Returns the `RootCauseAnalysis`. |
| GET | `/debug/investigations/{id}` | The stored investigation and its result. |
| GET | `/debug/investigations/{id}/observations` | Model turns, tool calls and observations, in order. `?timeline=true` returns every event. |

The repository must be indexed first (`POST /repositories/{id}/index`), because the index records its root path. That root must be inside `ALLOWED_REPOSITORY_ROOTS`. Both GET endpoints accept `?repository_id=`, which returns 404 for an investigation of another repository.

Errors: 422 for an invalid ID or an empty or oversized bug report, 413 for a body over 64 KB, 404 for an unknown repository, 403 outside the allowed roots, 503 if the LLM is not configured. A failed investigation is still stored, and its response carries the `investigation_id`: 504 for an LLM timeout, 503 for rate limits, 502 for provider errors or an invalid diagnosis, 500 for an internal error. `limit_reached` and `inconclusive` return 200.

The investigation runs synchronously inside the request, for up to `AGENT_MAX_DURATION_SECONDS`.

### Persistence

Migration `7d3a9c1e4b2f` creates `investigations` and `investigation_events`. Run `uv run alembic upgrade head`.

- The investigation row is committed before the loop starts.
- Each event is committed on its own, so a later failure keeps earlier events. `sequence` orders them.
- Event payloads hold model text, tool arguments and tool output as the model saw it. They hold no credentials.

### Tests

Every agent test uses `FakeLLM`, a scripted provider. A test can state the exact tool sequence, and inject invalid tools, malformed answers, empty responses, repeats, provider errors and timeouts. No test calls a real model.

### Benchmark

`benchmarks/agent/cases.json` holds five deliberately constructed bugs, each in its own small repository under `benchmarks/`:

| Case | Bug |
| --- | --- |
| `bug-001-average` | Division by zero on an empty list. |
| `bug-002-missing-record` | A missing user gives `None`, the route reads its attributes, and a broad `except` turns it into a 500. |
| `bug-003-pagination` | Floor division drops the partial last page. |
| `bug-004-discount` | The discounted price is computed but the original is returned. |
| `bug-005-config` | A broad `except` swallows an invalid `PORT` and returns the default. |

Expected answers stay in `cases.json`. The agent receives only the bug report.

```bash
uv run python benchmarks/agent/evaluate.py --dry-run   # validate cases, index repositories; no LLM
uv run python benchmarks/agent/evaluate.py             # real investigations; costs money
```

Metrics: root-cause accuracy (keyword groups), evidence precision (evidence pointing at expected files or symbols), evidence coverage (required evidence present), tool calls, iterations, latency and cost. A correct root cause without the required evidence is not a success.

Add `--max-cost-usd N` to stop before a case when the amount spent plus twice the costliest case so far would pass `N`.

Results on 2026-11-08 with `claude-opus-5-5`, effort `high`, `AGENT_MAX_DURATION_SECONDS=300`:

| Case | Root cause | Evidence coverage | Evidence precision | Tool calls | Time | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| bug-001-average | correct | 1.0 | 1.0 | 3 | 10 s | $0.03 |
| bug-002-missing-record | correct | 1.0 | 1.0 | 5 | 35 s | $0.13 |
| bug-003-pagination | correct | 1.0 | 1.0 | 3 | 25 s | $0.10 |
| bug-004-discount | correct | 1.0 | 1.0 | 5 | 28 s | $0.12 |
| bug-005-config | correct | 1.0 | 0.6 | 3 | 32 s | $0.13 |

Success rate 5/5, mean 3.8 tool calls and 4.4 iterations, mean 26 s, $0.51 in total. Every case ran its test and cited the failure. These are five small, single-bug repositories, so read the numbers as a check that the agent works, not as general accuracy.

### Known limitations

- Root-cause accuracy uses keyword matching. It is deterministic but coarse. An LLM judge can replace it later.
- Evidence precision counts an item without a file path as irrelevant. Execution evidence often has none, so bug-005 scores 0.6 although every item is relevant.
- Five small, single-bug repositories check the agent's mechanics. They do not show how it does on real repositories.
- The agent never sees the retrieval index's freshness. If the files changed after indexing, retrieval can show old lines while `read_file` shows new ones.
- A timed-out in-process tool keeps running in its worker thread until it reaches its own deadline. Python cannot stop a thread.
