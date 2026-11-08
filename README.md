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

## Patch generation and verification

`app/patching/` turns a completed investigation into a proposed fix and checks the fix in a disposable copy of the repository. A patch is only a proposal. It is `verified` only when every mandatory check passes. The original repository is never written, and nothing is committed or pushed. A human decides whether to accept the patch.

```
investigation -> baseline copy -> select tests -> baseline run
  -> [generate -> validate -> fresh copy -> git apply -> verify -> persist] x max_attempts
  -> report
```

| Module | Responsibility |
| --- | --- |
| `models.py` | Lifecycle statuses and transitions, limits, proposal, validation, check, comparison and execution models. |
| `generator.py` | One proposal from the Task 12 `LLMProvider` through a `submit_patch` tool. It never writes or runs anything. |
| `diff_parser.py` | Strict unified-diff parser, context anchoring and the canonical renderer. |
| `validator.py` | Path, scope, content and size rules. Only a valid diff yields a `ValidatedPatch`. |
| `workspace.py` | Isolated copies, snapshot hashes, ownership markers and retention. |
| `applier.py` | `git apply` with application-built arguments, conflict and unexpected-change detection. |
| `test_execution.py` | The `TestRunner` interface, the local pytest runner, ruff and JUnit XML parsing. |
| `container.py` | The container test backend: one locked-down Docker container per pytest run. |
| `test_selector.py` | Deterministic test selection, with a reason for each test. |
| `graph_tests.py` | Test suggestions from the Task 10 code graph. |
| `verification.py` | The baseline, the checks and the verdict. |
| `comparison.py` | Per-test before-and-after classification. |
| `service.py` | `PatchingService.generate_and_verify_patch(investigation_id)`. It orchestrates and has no HTTP. |
| `repository.py` | PostgreSQL persistence of attempts and verifications. |
| `routes.py` | The patch endpoints. |
| `budget.py` | The global LLM budget: requests, model time and cost across every attempt. |
| `evaluation.py` | Benchmark scoring. |

### Statuses

A proposal moves `generated -> validated -> applied -> verifying -> verified | rejected | inconclusive | failed`. A proposal that fails validation ends as `validation_failed` and is never applied. A proposal that does not apply, or that repeats an earlier attempt, ends as `rejected` straight from `validated`. Every other transition is refused, both in memory and in the repository.

- `verified`: the bug test failed in the baseline and passes now, no passing test broke, and every required check passed.
- `rejected`: a required check failed. Examples: the bug test still fails, a regression, a syntax error, a new lint violation, a conflict, or a hang when the baseline completed.
- `inconclusive`: correctness could not be established. Examples: the baseline did not reproduce the bug, a bug test was flaky, a tool was unavailable, or the time budget ran out.
- `failed`: an internal error, or the original repository changed during verification.

A skipped or unavailable check never counts as a pass.

### Checks

| Check | Rule |
| --- | --- |
| `patch_application` | `git apply --check`, then the apply. Git's `--numstat` must agree with the parser. No file outside the patch may change. |
| `syntax_check` | Modified Python files parse with `ast`. Nothing executes. |
| `original_bug_test` | Every test that failed in the baseline, among the tests the investigation saw fail, passes. |
| `regression_tests` | No targeted test goes from passing to failing or skipped, and no new test fails. |
| `ruff` | No new violations. Violations that existed before the patch are not blamed on it. |
| `full_test_suite` | The same comparison over the whole suite, when requested. |
| `original_repository_unchanged` | The original tree hashes the same as at the start. |

The baseline runs twice (`PATCH_BASELINE_RUNS`). Tests whose outcome differs between the runs are flaky and are left out of the comparison. A flaky bug test makes the result inconclusive. If the baseline does not reproduce the bug, the execution stops before it calls the model.

### Test selection

The selector takes tests from these sources, in priority order. It records every reason for each test.

| Priority | Source |
| --- | --- |
| 0 | Tests the investigation ran that failed. These reproduce the bug. |
| 1 | Other tests the investigation ran. |
| 2 | Test files named in the diagnosis or cited as evidence. |
| 3 | Test files that import an affected module or are named after it. Graph tests that call patchable code directly. |
| 4 | Test files one import hop away. Graph tests two or more calls away. |
| 5 | Other suggestions. |

The graph only suggests tests. `select_tests(suggested=...)` validates graph suggestions like any other path. A graph suggestion never gets a priority better than 3, so it never outranks the investigation's own tests.

1. Seeds are every function in the modifiable files, from the current source. A patch can only change those files.
2. The traversal follows incoming `CALLS` edges ("who calls this?"), one hop per query, for up to 3 hops (`PATCH_GRAPH_MAX_DEPTH`). It passes through helpers such as `make_service()`. Constructor calls resolve to `__init__`.
3. Test functions end a path. The score is 1.0 for symbols the diagnosis names and 0.6 for others. Each inferred edge multiplies it by 0.7, and each hop after the first by 0.6.
4. Files are ranked by score, then by path, and cut to 10 (`PATCH_MAX_GRAPH_TEST_FILES`).

Atlas uses Neo4j when it holds the repository and matches the current source. Every seed and every discovered test must have the same qualified name and line range in the baseline copy. If anything differs, the stored graph is stale, and Atlas discards the whole Neo4j result. When Neo4j is stale, unreachable or missing the repository, Atlas builds an in-memory graph of the current source with the same Task 10 builder. A graph failure never blocks verification. `test_selection.graph` in the execution records the graph source, seeds, suggestions and warnings.

### Safety

- Paths must be relative and use `/`. These are rejected: `..`, absolute paths, drive letters, backslashes, `.git`, `.env`, credential files, virtualenvs, caches, build output and any path through a symlink.
- Only the investigation's affected source files may be modified. Tests are read-only (`allow_test_changes=False`). Only `.py` and `.pyi` files can change. New files, deleted files, renames, mode changes and binary patches are rejected.
- The limits are: 5 files, 500 changed lines, a 200 KB diff, 1 MB per file, 3 attempts, 180 s of verification time and 100 KB of output per check. Each value is configurable.
- `git apply` receives the validator's canonical rendering, not the model's text. If a hunk has no leading or trailing context, git anchors it to the start or end of the file. The validator therefore finds each hunk in the real source and adds context lines from it. The validator recomputes hunk counts.
- Test and lint commands are built by the application. Their target paths are validated first. The model and the client never choose a command.
- Child processes run in the workspace with a minimal environment: no API keys, and HOME and TMPDIR inside the workspace. Each child runs in its own process group, which is killed on timeout. On POSIX each child also has CPU, memory, file-size and open-file limits. Output is capped.
- Source, tests and test output are treated as data in the prompt, and secrets are redacted.

### Container backend

`PATCH_TEST_BACKEND=container` runs every pytest invocation in its own disposable Docker container. Only the step that executes repository code moves into the container. Diff parsing, `git apply`, the `ast` syntax check and ruff only read code, so they stay on the host. The rest of the pipeline uses the same `TestRunner` interface (`run`, `available`, `identity`) and is unchanged.

Build the image once. It holds Python 3.12.13 and pytest 9.2.0, the same versions as the local runner:

```bash
docker build -f docker/test-runner.Dockerfile -t atlas-ai-test-runner:py3.12.13-pytest9.2.0 docker
```

| Control | How |
| --- | --- |
| Disposable | A new container per run, with a random name and the label `atlas.patching=1`. It is always removed with `docker rm -f`. |
| Non-root | `--user` is the host user, or 65534 if Atlas runs as root. Also `--cap-drop ALL` and `--security-opt no-new-privileges`. |
| No network | `--network none`: no routes and no DNS. Only the loopback interface is up. |
| Filesystem | `--read-only` root. The writable paths are the workspace copy at `/workspace`, an output folder for the JUnit report and a size-limited `/tmp` tmpfs. No other host path is mounted. |
| Limits | `--cpus`, `--memory` with swap equal to memory, `--pids-limit`, and `nofile` and `fsize` ulimits. |
| Timeout | Inside the container, coreutils `timeout` stops pytest, with SIGKILL after 5 s. Outside, a host deadline stops the CLI and `docker rm -f` kills the container and every process in it. Atlas reads Docker's `State.OOMKilled`, so a memory kill is reported as an error and a timeout as `timed_out`. |
| Secrets | The container gets a fixed `KEY=VALUE` list (HOME, TMPDIR and Python flags) and never the host environment. The docker CLI gets only what it needs to reach the daemon. |
| Results | Same command identity, same JUnit parsing and same statuses as the local runner. Each verification's `environment.test_backend` records the image ID and the controls. |

If the backend is configured but the image or daemon is unavailable, the request is refused with 503. Atlas never falls back to local execution.

The workspace folder (`PATCH_WORKSPACE_DIR`, by default the system temp folder) must be visible to the Docker daemon. On Docker Desktop, that means it must be inside a shared path in Settings, Resources, File sharing. If Docker cannot mount the workspace, the run fails closed: the baseline does not complete, and the result is `inconclusive`.

Repositories outside `AGENT_TRUSTED_TEST_ROOTS` are allowed only when both of these hold: the backend is `container`, and `PATCH_CONTAINER_ALLOW_UNTRUSTED=true`. They must still be inside `ALLOWED_REPOSITORY_ROOTS`. In that mode, ruff runs with `--isolated`, because the repository's ruff configuration is untrusted input.

`tests/patching/test_container.py` tests the isolation from inside the container. Its test files try to reach the network by IP and DNS, read a host secret and Atlas's own source, find the API key, write to the image, escalate privileges, start too many processes, exceed the memory limit and outlive a timeout. These tests skip without Docker; `REQUIRE_CONTAINER=1` makes a skip a failure. `evaluate.py --test-backend container` runs the reference benchmark this way, and all 16 verdicts match.

### LLM budget

Per-call timeouts do not bound the total, so each request also has a global LLM budget. The defaults are 6 model requests (`PATCH_MAX_LLM_REQUESTS`), 300 s of model time (`PATCH_MAX_LLM_SECONDS`) and $1.00 (`PATCH_MAX_LLM_COST_USD`). Repair calls count.

1. Before each call, Atlas reserves one request, the call's timeout and its worst-case cost. The worst case is the estimated input tokens at the highest input rate plus the full `LLM_MAX_TOKENS` output. A call that might not fit is not made.
2. Reserved amounts count as spent until the call ends, so concurrent calls cannot share the same remainder. A budget can have a parent, so one cap can cover several executions. `evaluate.py --max-cost-usd` uses one parent budget for the whole run.
3. After the call, the reservation is released and the actual cost from the reported usage is charged. If the actual cost is above the estimate, the call is flagged with `cost_estimate_exceeded`. A failed call without usage stays charged at its reservation, because the provider may have billed it.

The three stops are reported differently:

| Situation | Result |
| --- | --- |
| The budget does not allow the next call | `stop_reason="llm_budget_exhausted"`, with the limit named. The final attempt's status is kept, or `inconclusive` if no call was possible. Earlier attempts and their usage are kept. |
| The model call timed out | `failure_reason="llm_timeout"` (504). If the remaining model time had cut the call's timeout short, it is a budget stop instead. |
| The actual cost was above the estimate | Recorded as `cost_estimate_exceeded`. Not a failure. |

If the model's price is unknown, its cost cannot be capped. The request is then refused before any work, with 503. The execution's `llm_budget` report lists every call, with the estimated, actual and charged cost. Each proposal's `provenance` stores the budget state when that proposal was generated.

### Endpoints

| Method | Path | Result |
| --- | --- | --- |
| POST | `/debug/investigations/{id}/patches` | Body `{"max_attempts": 3, "run_full_test_suite": true}`. Generates and verifies a patch. Returns the final status, diff, per-check status and every attempt. |
| GET | `/debug/investigations/{id}/patches` | Every attempt, in order. |
| GET | `/debug/patches/{patch_id}` | The proposal, diff, validation result, status history and a verification summary. |
| GET | `/debug/patches/{patch_id}/verification` | Every check with its output, and the baseline and patched results with their comparison. |

The endpoints use the stored investigation. A client cannot send a diagnosis. Only `completed` investigations are eligible (409 otherwise). The repository must be indexed (404), inside `ALLOWED_REPOSITORY_ROOTS` and inside `AGENT_TRUSTED_TEST_ROOTS`, because verification runs its tests (403). `?repository_id=` and the body field `repository_id` return 404 for another repository's data.

`verified`, `rejected`, `inconclusive` and `validation_failed` return 200. The response includes `stop_reason` and the `llm_budget` report. An unknown model price returns 503. A `failed` execution returns 504 for an LLM timeout, 503 for rate limits or a workspace failure, 502 for provider errors or an invalid proposal, and 500 for internal or database errors. The request runs synchronously. `PATCH_LLM_TIMEOUT_SECONDS` bounds each attempt's model call. `PATCH_MAX_TOTAL_SECONDS` bounds the time spent in baseline and checks.

### Persistence

Migration `9e4b7c2d1a3f` creates `patch_proposals` and `patch_verifications`. Run `uv run alembic upgrade head`.

- Each attempt is committed when it is generated, and failed attempts are never overwritten. `execution_id` groups the attempts of one request.
- Each status change is checked, appended to `status_history` and committed.
- A verification row and its proposal's final status are written in one transaction.
- `provenance` records the provider, model, prompt version, usage and the source snapshot hash. `environment` records the interpreter, the tool versions, the test command and the timeout policy.

### Benchmark

`benchmarks/patching/cases.json` holds ten bugs. Each has its own small repository under `benchmarks/`. The first five are the Task 12 bugs, which need one change in one file. The other five are harder:

| Case | Category | What makes it hard |
| --- | --- | --- |
| `bug-006-region-pricing` | multi-file | Tax and shipping lookups are case-sensitive in opposite directions. Both lookups must change. Normalising only in `checkout.py` looks right but leaves the lookups broken. |
| `bug-007-durations` | ambiguous root cause | The bug is reported in the cache, but its cause is the shared duration parser, which request timeouts also use. Special-casing seconds in the cache looks right but leaves timeouts wrong. |
| `bug-008-score-report` | interacting bugs | Fixing the parser exposes a division by zero, and then a `max()` of nothing, in the summary. |
| `bug-009-leap-year` | plausible wrong fix | Dropping the century rule fixes 2000 but breaks 1900. Special-casing `2000` passes every repository test. |
| `bug-010-slugify` | plausible wrong fix | Switching to `\w+` keeps `é` and splits on nothing. Replacing only `é` passes every repository test. |

The model's input is the diagnosis and the source code. The diagnosis is either recorded in `diagnoses.json` or produced live by Task 12 with `--diagnosis agent`. The expected files, required tests, reference patches and held-out tests stay out of the model's input.

Held-out tests (`holdout/`) are never in the repository, so neither the model nor the verifier sees them. After an execution, the benchmark applies the final patch to a fresh copy, adds the held-out tests and runs them. They catch overfitting: a patch can be `verified` by Atlas and still fail them. Held-out tests check only behaviour the specification defines: docstrings, the bug report and existing tests.

Exploratory tests (`exploratory/`) check behaviour the specification leaves open. They run the same way and are reported (`exploratory_pass_rate`), but they never count toward the held-out rate or success. Failing one means the patch made a different defensible choice. One example is bug-008's trailing comma: the docstring defines blank input, not `"a:1,"`. That test was first a held-out test and was moved after the first live run, for that reason.

`--rescore RESULTS_JSON` recomputes a saved run's held-out, exploratory and summary metrics in place. It re-applies each recorded final patch and makes no model calls. It does not change the recorded patches, and it appends the previous summary to `rescores` for audit.

```bash
uv run python benchmarks/patching/evaluate.py                 # 30 reference runs: free, no LLM, no database
uv run python benchmarks/patching/evaluate.py --test-backend container
uv run python benchmarks/patching/evaluate.py --mode live --max-cost-usd 2               # recorded diagnoses
uv run python benchmarks/patching/evaluate.py --mode live --diagnosis agent --max-cost-usd 4   # Task 12, then Task 13
```

Reference mode sends 39 fixed patches through the full pipeline. The original 30 are a regression baseline. Their verdicts, the live result files' hashes and the git revision are frozen in `benchmarks/patching/results/baseline-2026-11-08.manifest.json`. The 14 new ones add the plausible wrong fixes above, two overfit patches and a scripted retry. In the retry, attempt 1 fixes only the parser and is rejected; attempt 2 fixes both bugs and is verified. Every run gets its expected verdict, attempt count and held-out result. `tests/patching/test_benchmark_cases.py` runs the same checks. It also checks that each case's root-cause keywords are not already satisfied by its bug report alone. That check caught weak keyword sets for bug-008 and bug-010.

#### Repair-retry experiment

Three cases (`bug-011` to `bug-013`, category `repair_retry`) are built so that the obvious fix breaks a rule that the repository's tests define and the bug report does not mention:

| Case | Obvious first fix | The test that rejects it |
| --- | --- | --- |
| `bug-011-split-bill` | Give the leftover cents to the last person. | `test_leftover_cents_go_to_the_first_people` |
| `bug-012-versions` | Compare integer tuples. | `test_release_candidates_compare_by_number`, which crashes on `-rc2` |
| `bug-013-unique-emails` | Lower-case every address. | `test_original_spelling_is_kept` |

With tests in the prompt, a strong model can read the rule and fix the bug on the first attempt. `--context-tests 0` (`PatchLimits.max_context_test_files`) withholds test contents from the prompt. Every test still runs. The model then learns a test-encoded rule only from the failure output, as it would in a repository too large for its tests to fit in the prompt. Run each arm separately:

```bash
uv run python benchmarks/patching/evaluate.py --mode live --context-tests 5 --case bug-011-split-bill ...   # control
uv run python benchmarks/patching/evaluate.py --mode live --context-tests 0 --case bug-011-split-bill ...   # test-blind
```

A case counts as `repaired_by_retry` only when the record proves every step:
1. Attempt 1 failed, and its failing checks are recorded.
2. The final attempt's `feedback_reasons` contain attempt 1's exact failure reasons.
3. The final diff differs from the first one.
4. The final patch is verified, and its required and held-out tests pass.

`repair_evidence` in each score holds this chain. A first-attempt success can never count as a repair. Every attempt also stores `feedback_from` and `feedback_reasons`, in memory and in each proposal's `provenance`. The reference set includes a plausible first attempt and a scripted retry for each case. Tests check that the second prompt contains the first attempt's real failure text.

Results on 2026-11-08: `claude-opus-5-5`, recorded diagnoses, up to 3 attempts, and the cases bug-008, bug-011, bug-012 and bug-013. Each arm had a $1 cap.

| Arm | First-attempt failures | Repaired by retry | Final verified | Held-out | Regression rate | Cost | Cost per success | Cost per repair |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Control (`--context-tests 5`) | 0/4 | 0 | 4/4 | 4/4 | 0 | $0.12 | $0.031 | n/a |
| Test-blind (`--context-tests 0`) | 1/4 | 1 (bug-012) | 4/4 | 4/4 | 1 of 5 attempts | $0.16 | $0.040 | $0.083 |

The bug-012 repair, from `2026-11-08-retry-blind-opus-5-5.json`:
1. Attempt 1 compared integer tuples. It fixed the reported bug (both required tests passed) and was rejected, because `test_release_candidates_compare_by_number` went from passing to `ValueError: invalid literal for int() with base 10: '0-rc2'`.
2. Attempt 2 received exactly that reason (`feedback_from=[1]`). It added a pre-release component, and the model's assumptions said why. It was verified, and both held-out tests pass, including `rc10` against `rc9`.
3. This took two model calls and cost $0.083. The record's `repair_evidence` holds the full chain.

The test-blind arm was then repeated three more times, with a $1 total cap; those runs cost $0.32. `2026-11-08-retry-blind-aggregate.json` combines the four runs, 16 case-runs in all:

| Case | Run 1 | Run 2 | Run 3 | Run 4 |
| --- | --- | --- | --- | --- |
| bug-008-score-report | verified | verified | verified | verified |
| bug-011-split-bill | verified | verified | verified | verified |
| bug-012-versions | rejected > verified (repaired) | verified, **fails held-out** | rejected > verified (repaired) | rejected > verified (repaired) |
| bug-013-unique-emails | verified | verified | verified | verified |

- Overall: 3 first-attempt failures in 16 case-runs, all on bug-012. All 3 were repaired on attempt 2, with the full evidence chain, so the retry success rate is 3/3.
- Final verified: 16/16. Success: 15/16.
- 3 of the 19 applied attempts caused a regression, and every one was caught and repaired.
- Total $0.48: $0.076 per successful repair and $0.032 per success.
- On bug-012, the first attempt failed in 3 of the 4 runs.

Run 2's bug-012 patch is the most important result. The model avoided the `-rc` crash by falling back to plain string comparison for any non-numeric version. That passes every repository test, because `"rc2" > "rc1"` holds as strings too, so Atlas verified it on the first attempt. It fails the held-out `rc10` against `rc9`, which the existing test `test_release_candidates_compare_by_number` defines. That is a wrong patch Atlas accepted, and only the held-out test caught it. Real repositories have no held-out tests, so a reviewer is still required. It also shows that a defensive first attempt can avoid the failure that would have triggered a useful retry.

What this shows and what it doesn't:
- With tests visible, Opus needed no retries. Without them, it inferred the hidden rule every time in bug-011 (first person) and bug-013 (keep the spelling). It inferred it in none of the four runs of bug-012.
- The feedback loop works with a real model, and repeatably: 3/3 retries succeeded on the one case that produced them.
- The rates come from one case with retries, four runs and small samples. They are evidence of the mechanism, not a general repair rate. More retry cases whose hidden rules a model cannot infer are needed.

Metrics:
- Diagnosis accuracy, in agent mode, using the Task 12 root-cause keywords.
- Patch generation, validation and application rates.
- Bug-fix rate: the case's required tests pass.
- Regression-free rate, and the regression rate per applied attempt.
- Verified rate, first-attempt verified rate and held-out pass rate.
- Mean attempts, cases with retries, first-attempt failure rate, retry success rate (cases verified after a failed first attempt) and `repaired_by_retry` (evidence-backed).
- Cost per successful repair and cost per success. Both include failed attempts.
- Latency and cost, with diagnosis and patching recorded separately.
- A breakdown by category.

Success means verified, with the required tests passing, only expected files modified and the held-out tests passing. A green suite alone is not success. One reference regression patch makes the bug test pass and is still rejected. The two overfit references are verified by Atlas and still fail the held-out tests.

Results on 2026-11-08 with `claude-opus-5-5`, effort `high`, recorded diagnoses (`--mode live --max-cost-usd 2`):

| Case | Status | Attempts | Lines | Time | Cost |
| --- | --- | --- | --- | --- | --- |
| bug-001-average | verified | 1 | +2/-0 | 8.0 s | $0.025 |
| bug-002-missing-record | verified | 1 | +2/-0 | 6.9 s | $0.027 |
| bug-003-pagination | verified | 1 | +1/-1 | 5.1 s | $0.021 |
| bug-004-discount | verified | 1 | +1/-1 | 5.3 s | $0.021 |
| bug-005-config | verified | 1 | +7/-5 | 8.1 s | $0.029 |

Results on 2026-11-08 for all ten cases, `claude-opus-5-5`, effort `high`, `--max-cost-usd 4` in total for both runs:

| Run | Diagnosis | Verified | First attempt | Held-out | Success | Regressions | Retries | Cost | Mean latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `--diagnosis agent` (Task 12, then Task 13) | 10/10 | 10/10 | 10/10 | 7/7 | 10/10 | 0 | 0 | $1.40 ($1.12 diagnosis, $0.28 patching) | 35.6 s |
| Recorded diagnoses | n/a | 10/10 | 10/10 | 7/7 | 10/10 | 0 | 0 | $0.25 | 6.9 s |

Exploratory tests: in the agent run, the bug-008 patch returns early on blank text and still raises on `"a:1,"`. That is a choice the model stated as an assumption. In the recorded run, the patch skips blank items. Both runs were rescored after the trailing-comma test moved to the exploratory set. Before the move, the agent run was 9/10 success with held-out 6/7. The recorded patches did not change.

Read these numbers with three limits:
- **No retries.** No case needed a second attempt, so real-model repair from failure feedback is still measured only by the scripted reference retry.
- **Generous keyword match.** The bug-008 agent diagnosis is counted correct, but it named only the parser bug. It mentioned `summarize` only as the caller, and the keyword matched anyway. The patch generator found the second bug by reading `report.py`.
- **Small benchmark.** These are ten small, constructed repositories.

The per-attempt records are in `benchmarks/patching/results/`. That run came before the global LLM budget, which has no effect on single-attempt cases.

Verified rate 5/5. Every patch changed only the diagnosed file, and none touched a test. Mean 6.7 s, $0.12 in total. No diff needed context anchoring.

### Known limitations

- With the local backend (the default), a temporary directory is not a sandbox. Test code runs as this user and can reach the network and the rest of the filesystem. Only verify trusted repositories (`AGENT_TRUSTED_TEST_ROOTS`). `RLIMIT_AS` (memory) is not enforced on macOS.
- A container is an isolation boundary, not a guarantee. It shares the host kernel, so a kernel or runtime vulnerability can break out. Treat `PATCH_CONTAINER_ALLOW_UNTRUSTED` as a security-sensitive setting. For hostile code, run the worker on a separate VM or host, and consider gVisor or Kata.
- The container's `--user` is the host user's UID, so that the bind-mounted workspace is writable. Inside the container that UID has no privileges and no capabilities. On a host where that UID owns sensitive files, run Atlas as a dedicated user.
- The image contains only pytest. If a baseline test fails only with `ModuleNotFoundError` for a module that is not part of the repository, that is an environment problem. The run stops as `inconclusive` before calling the model, so a patch that deletes the import can never be "verified". Per-repository images with dependencies come later.
- Only Python source files can be patched, and only test files named `test_*.py` or `*_test.py` are selected.
- Graph test discovery follows `CALLS` edges only. It misses tests that reach the code through pytest fixtures, dynamic dispatch or calls the Task 10 resolver leaves unresolved. Import association still covers those tests at the file level.
- The Neo4j staleness check compares line ranges of the seeds and the discovered tests. An edit inside a function that keeps its line range is not detected. The edges from such an edit can be outdated.
- Atlas can only verify against the tests a repository has. A patch that special-cases the reported input can be `verified`, as the two overfit references show. The held-out tests measure this in the benchmark, but real repositories have no held-out tests. A human still reviews every patch.
- The live results below use recorded diagnoses on five small, single-bug repositories. They show that the pipeline works with a real model. They are not a measure of patch quality on real code.

## Evaluation laboratory (Task 14)

`app/evaluation/` turns the diagnosis and patch pipeline into a reproducible experiment platform. It calls the existing services (Task 12 agent, Task 13 patching, the global LLM budget, the local or container test backend). It has no agent loop of its own.

```
configuration -> run (cases, attempts, evidence) -> metrics (derived) -> comparison / baseline check
```

| Module | Responsibility |
| --- | --- |
| `models.py` | Tables: configurations, runs, case results, attempts, metric calculations and snapshots, baselines. |
| `configuration.py` | Validated, secret-free, versioned configurations. Prompt hashes are pinned. |
| `runner.py` | Executes cases with the existing services. Saves each case and each attempt as it finishes. |
| `service.py` | Validates a request, prevents duplicates, starts a background thread, recovers interrupted runs. |
| `metrics.py` | Pure metric calculation from stored rows. No database, no HTTP. |
| `comparisons.py`, `regression.py` | Run comparison and baseline regression detection. |
| `importer.py` | Imports the historical JSON result files. |

### Rules the data model enforces

- `NULL` means not recorded or not applicable. A case whose diagnosis failed has `final_verified = NULL`, not `false`.
- Money is `NUMERIC(12,6)`. `cost_kind` says `actual` (provider-reported tokens times the price table) or `estimated`. Unknown cost is `NULL`, never `0`. Reference runs make no model calls and record no cost.
- Foreign keys never cascade. Database triggers reject changes to a configuration, to a finished run and its cases and attempts, and to metric snapshots and baselines. A change is a new row.
- Recalculating metrics adds a new calculation version with a reason and an evidence hash. It never edits case or attempt rows. An equal hash proves the evidence did not change.
- Reference mode scores several fixed patches per case, so a case row is unique per `(run, case_id, variant)`; live runs use the variant `live`.

### Metrics

Each metric is `numerator / denominator` over an explicit population. A field that is `NULL` is left out of that denominator. A zero denominator gives `null`, not 0%.

| Metric | Numerator | Denominator |
| --- | --- | --- |
| `diagnosis_accuracy` | correct diagnoses | scored diagnoses |
| `patch_validity_rate` | valid patches | attempts that produced a patch |
| `patch_application_rate` | applied patches | valid patches |
| `first_attempt_verified_rate` | cases verified on attempt 1 | cases with an attempted patch |
| `final_verified_rate` | cases finally verified | cases with an attempted patch |
| `heldout_pass_rate`, `exploratory_pass_rate` | passing cases | cases with those tests |
| `regression_rate` | applied attempts that broke a passing test | applied attempts |
| `retry_success_rate` | cases repaired after a failed first attempt | cases whose first attempt failed and were retried |
| `success_rate` | verified, expected files only, held-out not failing | cases with an attempted patch |
| `completion_rate` | cases whose pipeline completed | all selected cases |
| `average_attempts`, `average_latency_seconds`, `average_cost_per_case` | sums | cases with that value |
| `cost_per_successful_repair` | all recorded cost | successful cases |

Cases that never ran (budget exhausted, cancelled, interrupted) stay in `completion_rate` and nowhere else. They are never failures and never successes. Breakdowns exist by category, model, retrieval strategy, test visibility, configuration and run.

### API

| Method | Path | Purpose |
| --- | --- | --- |
| POST, GET | `/evaluations/configurations`, `/{id}`, `/{id}/clone` | Create, list, read, clone (a new version) |
| GET | `/evaluations/benchmarks` | Selectable cases and the benchmark version |
| POST | `/evaluations/run` | Validate and start a run in the background (202) |
| GET | `/evaluations`, `/{id}`, `/{id}/cases`, `/{id}/cases/{case_id}`, `/{id}/metrics` | Records and metrics |
| POST | `/evaluations/{id}/recalculate`, `/{id}/cancel` | New metric calculation; cancel an active run |
| POST | `/evaluations/compare` | Compare 2-10 runs |
| POST, GET | `/evaluations/baselines`, `/{id}`; POST `/evaluations/{id}/regression-check` | Baselines and threshold checks |

A live run needs `benchmark_ids`, an explicit `max_cost_usd` no larger than the configuration's budget, and `confirm_paid: true`. One budget covers diagnosis, patch and repair calls; a call that might not fit is not made. Only one evaluation runs at a time (409 otherwise), and an `idempotency_key` makes a repeated request return the same run. Opening the dashboard or listing runs never calls a model.

Run statuses: `completed` (every selected case has a finished record), `partial` (budget exhausted, interrupted, or cases left incomplete), `failed`, `cancelled`. A restart closes runs it interrupted as `partial` or `failed`.

### Configurations

A configuration fixes the model (`provider`, `model_id`, `effort`, `max_tokens`), retrieval (`strategy`: `hybrid`, `keyword`, `vector`, `graph`, `none`; `graph_enabled`; `embedding_model`), agent limits and tools, patch attempts, the test backend (`local` or `container`, required) and visibility (`blind` shows the model no test files), and the budget. Unknown fields are rejected, so no API key can be stored. Strings are also scanned for key-shaped values and for the live secrets. The server adds hashes of the diagnosis prompt, the patch prompt and the tool schemas. A live run refuses a configuration whose prompts changed since it was created: clone it.

`graph_enabled=false` removes graph retrieval from the agent and disables graph test discovery in patching. Retrieval settings affect a run only when the agent diagnoses (`diagnosis_mode: agent`); with recorded diagnoses they are recorded but unused, and the run's `environment.retrieval_applied` says so.

### Comparison and regressions

Comparisons show raw counts, absolute and relative differences and sample sizes. Under 30 cases, every difference carries a warning. Atlas never reports statistical significance. Only cases present in every run are compared case by case; a missing case is listed as missing. Different benchmark versions are flagged.

A baseline copies a completed run's configuration and metrics into a new row and leaves the run untouched. A regression check refuses a different benchmark version and uses only shared cases. Thresholds are request values with defaults in `RegressionThresholds`: final verified rate drop 10 points, held-out drop 10 points, regression rate rise 5 points, category verified drop 20 points, cost per repair rise 25%, latency rise 25%. Each finding names the metric, both counts, the threshold and the supporting case IDs.

### Historical results

```bash
uv run python -m app.evaluation.importer                       # every result file in benchmarks/patching/results
uv run python -m app.evaluation.importer 2026-11-08-live-opus-5-5.json
```

Files are only read. The file name and SHA-256 are stored, and importing the same bytes again does nothing. Fields a file never recorded stay `NULL` (the oldest file has no categories, held-out results or diagnosis scores). Imported runs are marked historical, use `benchmark_version = unknown-historical` (so a current baseline refuses them), and are checked against the file's own summary: case count, verified and success rates, total cost. A mismatch aborts the import.

### Dashboard

```bash
cd frontend && npm install && npm run dev      # http://localhost:5173, proxies /evaluations to ATLAS_API (default :8000)
npm test                                        # formatting helpers
```

Pages: overview, start evaluation (reference or live, explicit budget, confirmation), evaluation detail (polls while a run is active, metrics with denominators, per-case table, regression check), comparison (side by side, best value highlighted, category and case breakdown), and case investigation (diagnosis, evidence, every patch attempt with its diff, feedback and checks, held-out results). Every value comes from the API; unknown shows as `n/a`.

### Limits of this milestone

- Exact LLM output is not reproducible. A run records the conditions (configuration snapshot, prompt hashes, benchmark version, commit, backend and image ID) and keeps every output.
- Execution is one background thread in one process. There is no queue and no second worker.
- The benchmark has 13 small constructed bugs. Differences between configurations on this sample are observations, not evidence that one model is better.
- Agent diagnoses are stored as `investigations` rows and linked from the case. Recorded diagnoses have no row.
- Terminal runs cannot be deleted by design, so tests use only the rollback-wrapped session and never commit evaluation rows.
