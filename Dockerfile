# Test-deployment image for agent-dashboard (see ADR 0002).
# Multi-arch: TARGETARCH selects the Docker static client binary (amd64/arm64).
ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim AS base

ARG DOCKER_VERSION=28.3.0
ARG TARGETARCH

# Install the Docker CLI (static client binary only, no daemon) for the
# target architecture, then drop privileges: the gateway shells out to
# `docker`, and socket access comes from the compose `group_add` entry.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-amd64}" in \
         amd64) DOCKER_ARCH=x86_64 ;; \
         arm64) DOCKER_ARCH=aarch64 ;; \
         *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && docker --version \
    && useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# Dependency layer first for build caching. uv.lock pins every version;
# `uv sync --locked` fails loudly when pyproject.toml and uv.lock drift.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev \
    && uv pip list | grep -Ei 'fastapi|uvicorn' || true

COPY --chown=appuser:appuser src ./src

ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8000
USER appuser
CMD ["agent-dashboard"]
