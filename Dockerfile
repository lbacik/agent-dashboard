# Container image for agent-dashboard: a read-only fleet view on a trusted
# LAN without sign-in (ADR 0002), or LAN mode behind a reverse proxy on the
# same host (ADR 0012). See docker-compose.yml.
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
    && chown -R appuser:appuser /app

# State directory (Control audit, Operator account store), the compose
# `state` volume's mount point. A fresh named volume copies this owner, so
# appuser can write to it although the root filesystem is read-only.
RUN mkdir -p /state \
    && chown appuser:appuser /state \
    && chmod 0700 /state

ENV PATH="/app/.venv/bin:${PATH}"
# Unbuffered stdout: startup lines reach `docker logs` when they are written.
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
USER appuser
# Process liveness only (not fleet or gateway readiness): GET /healthz
# answers 200 `ok` without a sign-in session in either mode, behind the
# Host check. The probe uses the venv Python standard library only (no
# curl in the image); `-B` keeps bytecode out of the read-only filesystem
# and the urllib timeout sits under the 5 s healthcheck timeout, so the
# check passes under `read_only`, `cap_drop: ALL`, `no-new-privileges`
# as appuser. Compose inherits this; `docker compose up -d --wait` waits
# for it.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s CMD ["python", "-B", "-c", "import os,sys,urllib.request\nport=os.environ.get('AGENT_DASHBOARD_PORT') or '8000'\ntry:\n code=urllib.request.urlopen('http://127.0.0.1:%s/healthz'%port,timeout=4).getcode()\nexcept Exception:\n raise SystemExit(1)\nraise SystemExit(0 if code==200 else 1)"]
CMD ["agent-dashboard"]
