# Atlas AI patch-verification worker: runs a repository's pytest suite in isolation.
#
# The image holds only Python and pytest. Every security control is applied at
# `docker run` time by app/patching/container.py (no network, read-only root,
# non-root user, no capabilities, CPU/memory/PID limits). The image has no
# credentials and is never given any.
#
#   docker build -f docker/test-runner.Dockerfile -t atlas-ai-test-runner:py3.12.13-pytest9.2.0 docker
#
# Keep Python and pytest at the versions Atlas uses locally, so local and
# container results are comparable.
FROM python:3.15.0-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install "pytest==9.2.0" \
    && rm -rf /root/.cache

WORKDIR /workspace

# Never root by default. Atlas always starts the container with an explicit --user, so this only matters if someone
# runs the image without Atlas's flags.
USER 65534:65534
