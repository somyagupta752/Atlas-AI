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
