# Security boundaries

The rules that must stay true. Each one names the code that enforces it and the test that fails if it breaks.

## 1. Untrusted code runs only in a worker container

- Verification of a repository outside `AGENT_TRUSTED_TEST_ROOTS` is refused with the local backend.
- With `PATCH_TEST_BACKEND=container`, pytest runs in one disposable container per run
  (`app/patching/container.py`). Only pytest runs there; diff parsing, `git apply`, `ast` checks and ruff only read code.
- Production must use the container backend and an empty trusted-roots list (enforced by 15.3).

## 2. The worker container

Built by `ContainerTestRunner.docker_command` as an argument list. There is no shell.

| Control | Setting |
| --- | --- |
| Network | `--network none` |
| Root filesystem | `--read-only`; `/tmp` is a size-limited `tmpfs` |
| Privileges | `--cap-drop ALL`, `--security-opt no-new-privileges`, never `--privileged` |
| User | host UID or 65534; never 0 |
| Memory and swap | `--memory M --memory-swap M` (no swap) |
| CPU, processes | `--cpus`, `--pids-limit` |
| Files | `--ulimit nofile`, `--ulimit fsize` |
| Time | `timeout -k 5 N` inside, a host deadline and `docker rm -f` outside |
| Mounts | exactly two bind mounts: the workspace copy and the output directory. No Docker socket, no home directory. |
| Environment | a fixed list (`HOME`, `TMPDIR`, Python flags). Never the host environment. |
| Identity | name `atlas-test-<random>`, label `atlas.patching=1` |

Tests: `test_container_command_has_every_isolation_flag_and_no_host_access`,
`test_host_secrets_in_the_environment_never_reach_the_container_command`, and the container test that runs
hostile code in the real image.

### Docker access for the workers

The API never mounts the Docker socket. It reaches a filtering proxy, and the proxy reaches the daemon. A proxy cannot
inspect what a `create` request asks for, so with a rootful daemon a compromised API could still obtain host root. The
decided design is a **rootless daemon under a dedicated account on a dedicated VM**, so the same request yields only that
account's privileges (`rootless-worker-design.md`). It is **not validated**: until `worker-validation-plan.md` is signed off,
do not run untrusted repositories.

## 3. The original repository is never written

Workspaces are copies. The source tree is hashed before and after every case, and a change aborts the run.
Symlinks that leave the repository are not copied. Atlas proposes patches; it has no endpoint that applies
one to the original.

## 4. Paths

- A client path is accepted only if its resolved form is inside an allowed root. The check runs before
  the existence check.
- Patch paths must be relative, free of `..` and backslashes, and must not pass through a symlink.
- Repository IDs match a fixed pattern. They are never used as file paths.

## 5. Repository content is evidence, not instructions

Source text, comments and test output are passed to the model as tagged data. The model can call only
read-only tools. Its patch is validated (allowed files, size, no test edits), applied in a copy and verified
by tests it cannot change. A model that obeys injected text can still produce a wrong patch, so a person
reviews every patch.

## 6. Secrets

- Keys are `SecretStr` read from the environment. They are never put in a configuration snapshot, a
  response, an error message or a worker.
- Tool output is redacted before it reaches the model or a record.
- Production refuses insecure default secrets (15.3).

## 7. Spending

Every model call is reserved against a budget before it is made. A run has a hard cap that covers diagnosis,
patches and repairs. A live run needs an explicit budget and confirmation.

## 8. Data integrity

Evaluation configurations, finished runs, their cases and attempts, metric snapshots and baselines cannot be
changed or deleted (database triggers). Recalculation adds a new version.

## What is not a boundary

- The local test backend. It is a child process with the API's privileges.
- A container against a kernel exploit. Use gVisor, Kata or a dedicated VM for hostile multi-tenant code.
- The model provider. Anything sent to it leaves your control.
