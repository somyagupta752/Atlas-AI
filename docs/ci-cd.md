# CI/CD

Four workflows in `.github/workflows/`. None deploys. None uses a secret other than the read-only `GITHUB_TOKEN`
(`tests/deploy/test_workflows.py` enforces both). Deploying is a person running the commands in `deployment.md`.

| Workflow | When | What it does | Blocks a merge |
| --- | --- | --- | --- |
| `ci.yml` | every PR, push to `main` | Ruff (with the bandit `S` rules), lockfile check; backend tests against real PostgreSQL, Neo4j and Docker workers (container tests cannot skip); migrations up, down to base, up, and `alembic check`; frontend tests and build; **browser tests (Playwright, real API, throwaway database)**; Dockerfile lint; builds the API, backup and frontend images and checks them (non-root, tools present, no tests or secrets, backup directory mode, revision baked in); validates both compose files | yes |
| `security.yml` | every PR, push to `main`, **weekly** | `pip-audit` on locked runtime dependencies; `npm audit` on production dependencies (HIGH+); gitleaks over the full history; Trivy on each built image (fixable HIGH/CRITICAL) and on Dockerfiles and compose (misconfiguration) | yes |
| `codeql.yml` | every PR, push to `main`, weekly | CodeQL `security-extended` for Python and JavaScript | results in the Security tab |
| `build-images.yml` | manual only | Builds the three release images with a version tag, scans them, and **optionally** publishes to the repository's GHCR. Never deploys | n/a |

`dependabot.yml` opens weekly update PRs for Actions, Python (uv), npm, and all three Dockerfiles. Major npm versions are
left to a person.

## Why the schedule matters

New advisories appear without any change to the repository. A green build last month says nothing about today, so
`security.yml` runs every Monday and rebuilds the images without cache, so base-image updates are picked up.

## Run the same checks locally

```bash
uv run ruff check .
uv run pytest -m "not slow and not worker_host" -q
(cd frontend && npm ci && npm test && npm run build)
docker run --rm -v "$PWD":/repo -w /repo rhysd/actionlint:1.7.7                                   # workflow syntax
docker run --rm -i -v "$PWD/.hadolint.yaml":/.hadolint.yaml hadolint/hadolint:v2.12.0 hadolint --config /.hadolint.yaml - < Dockerfile
docker run --rm -v "$PWD":/repo:ro zricethezav/gitleaks:v8.21.2 detect --source /repo --config /repo/.gitleaks.toml --redact
uvx pip-audit -r <(uv export --frozen --no-dev --no-emit-project --no-hashes) --no-deps --disable-pip
(cd frontend && npm audit --omit=dev --audit-level=high)
```

For images, build then scan a tarball (no Docker socket is mounted into the scanner):

```bash
docker build -t atlas-ai-api:scan . && docker save atlas-ai-api:scan -o /tmp/api.tar
docker run --rm -v /tmp:/work aquasec/trivy:0.58.1 image --input /work/api.tar --severity HIGH,CRITICAL --ignore-unfixed
```

## Exceptions

- **Trivy:** `.trivyignore` is empty. An entry needs the ID, why it does not apply, who accepted it, and an expiry date.
- **gitleaks:** `.gitleaks.toml` allowlists only `tests/`, where fake credentials prove that redaction works. A real
  secret anywhere else fails the build (checked: a planted token outside `tests/` is detected).
- **Hadolint:** `DL3006` (it cannot read `FROM ${ARG}`; every base image is a pinned ARG) and `DL3008` (exact apt versions
  break on every Debian security update; the base tag is pinned and Trivy scans the result).
- **Frontend dev tools:** `vite` and `vitest` have advisories whose fixes are major upgrades. They run on a developer's
  machine and in CI, never in an image. The audit step for all dependencies is reported, not blocking.
- **react-router 6.x:** two MODERATE advisories remain, fixed only in v7 (breaking). One is an open redirect through
  `<Link>`/`useNavigate` with an untrusted URL; Atlas never passes an untrusted URL to either (the login redirect target
  is validated in `frontend/src/auth/safeRedirect.js` and tested). The other concerns server-side rendering, which Atlas
  does not use. Plan: migrate to v7 as a separate change.

## Browser tests

`ci.yml` runs the Playwright suite in its own job with only a PostgreSQL service (`docs/e2e-testing.md`). A failure uploads the
HTML report and traces for seven days.

## Not covered

- Actions are pinned to release tags, not commit SHAs. Dependabot keeps them current; pin to SHAs if your threat model
  includes a compromised action repository.
- Nothing here runs on a rootless worker host. The `worker_host` tests are excluded from CI on purpose and run on the
  staging VM (`security/worker-validation-plan.md`).
- The `slow` tests (extra Neo4j containers for the dump and restore round trip) are excluded from pull requests.
- CI has not run on GitHub yet. Every check was run locally with the same tools and versions; the first real run may
  differ in runner details.
