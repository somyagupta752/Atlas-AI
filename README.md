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
