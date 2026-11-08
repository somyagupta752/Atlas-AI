# syntax=docker/dockerfile:1.7
# Atlas AI backend. Multi-stage: dependencies are installed from the lockfile in a builder stage; the runtime image
# holds the virtualenv and the application, runs as a non-root user, and contains no build tools, tests or secrets.
#
#   docker build -t atlas-ai-api:local .                                  # the API (default target, last stage)
#   docker build --target backup -t atlas-ai-backup:local .               # the same plus PostgreSQL 16 client tools
#   docker build --build-arg VERSION=1.2.3 --build-arg REVISION=$(git rev-parse HEAD) -t ... .
ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.7
ARG DOCKER_CLI_IMAGE=docker:29.9.3-cli

FROM ${UV_IMAGE} AS uv

FROM ${DOCKER_CLI_IMAGE} AS docker-cli

# --- builder ---------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=uv /uv /usr/local/bin/uv
# The virtualenv is created at /app/.venv and used from /app/.venv: a virtualenv records its own absolute path in
# the scripts it installs (uvicorn, alembic), so building it elsewhere and copying it breaks every entry point.
WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: exactly the locked versions. --no-dev: no pytest or other development tools.
RUN uv sync --frozen --no-dev --no-install-project

# --- common runtime base ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.version="${VERSION}" org.opencontainers.image.revision="${REVISION}"
ENV ENVIRONMENT=production \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATCH_WORKSPACE_DIR=/var/lib/atlas/workspaces \
    ATLAS_VERSION=${VERSION} \
    ATLAS_REVISION=${REVISION}
# git: `git apply` validates patches. The Docker CLI talks to the worker backend; the image has no Docker daemon,
# and the host's Docker socket is never mounted here (see docs/deployment.md).
# `apt-get upgrade` applies the distribution's security fixes that are newer than the base image's snapshot; without it
# the image ships whatever was vulnerable on the day the base image was built (Trivy flagged 8 HIGH findings).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 atlas \
    && useradd --system --uid 10001 --gid atlas --home-dir /home/atlas --create-home --shell /usr/sbin/nologin atlas \
    && mkdir -p /var/lib/atlas/workspaces /var/lib/atlas/backups \
    && chown -R atlas:atlas /var/lib/atlas \
    # The backup tool refuses a directory other users can read. A new named volume copies this mode.
    && chmod 700 /var/lib/atlas/backups
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY --from=builder --chown=atlas:atlas /app/.venv /app/.venv
COPY --chown=atlas:atlas alembic.ini main.py ./
COPY --chown=atlas:atlas alembic ./alembic
COPY --chown=atlas:atlas app ./app
COPY --chown=atlas:atlas benchmarks ./benchmarks

# --- backup job: needs pg_dump/pg_restore of the SERVER's major version (16); Debian's own client is 15 -----------
FROM base AS backup
USER root
# postgresql-common's helper adds the signed PostgreSQL (PGDG) apt repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-common \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y postgresql-common \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
USER atlas
CMD ["python", "-m", "app.ops.backup", "backup"]

# --- API (the default target must be last) --------------------------------------------------------------------
FROM base AS runtime
LABEL org.opencontainers.image.title="atlas-ai-api"
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
# Exec form: uvicorn is PID 1 and receives SIGTERM. It stops accepting requests; the application then cancels active
# evaluations and removes its worker containers (lifespan shutdown) within the graceful-shutdown window.
# Behind the dashboard's reverse proxy the client address comes from X-Forwarded-For. uvicorn honours that header only
# from the addresses in FORWARDED_ALLOW_IPS (set by the deployment); without it every user would share one rate limit.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30", "--no-server-header"]
