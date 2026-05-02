# kvwarden Dockerfile — multistage, slim base.
#
# Builder stage installs the package + dev extras into a venv, runtime
# stage copies just the venv and the kvwarden console script. The runtime
# image stays small (no compilers, no build deps) and runs as a non-root
# user. curl is kept for the HEALTHCHECK probe.
#
# Build:
#   docker build -f docker/Dockerfile -t kvwarden:local .
#
# Run (standalone — assumes vLLM reachable on localhost:8001 and a config
# in /etc/kvwarden/config.yaml; the compose bundle wires this up):
#   docker run --rm -p 8000:8000 \
#     -v $(pwd)/docker/quickstart-compose.yaml:/etc/kvwarden/config.yaml:ro \
#     kvwarden:local serve --config /etc/kvwarden/config.yaml

# ── Builder ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# build-essential covers compiled wheels (numpy, pandas, aiohttp) when no
# manylinux wheel exists for the python+platform combination.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv

WORKDIR /src

# Copy only what's needed for `pip install`. Source goes in last so a
# code-only edit doesn't bust the dep-install layer.
COPY pyproject.toml README.md ./
COPY src ./src

# `[dev]` is the broadest non-vllm extra (pytest, ruff, etc.). vLLM is
# NOT installed in the kvwarden image — it lives in the vllm/vllm-openai
# service in the compose bundle, and the kvwarden container reaches it
# via the shared network namespace. See docker-compose.yml for the wiring.
RUN pip install --upgrade pip && pip install -e ".[dev]"

# ── Runtime ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# curl for HEALTHCHECK; tini for proper signal handling under PID 1.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 kvwarden \
    && useradd  --system --uid 1001 --gid kvwarden --home-dir /app --shell /usr/sbin/nologin kvwarden

# Pull the venv (binaries + site-packages + entry-point script) from the
# builder. The kvwarden console script lives at /opt/venv/bin/kvwarden
# because pip installs editable extras' scripts there.
COPY --from=builder /opt/venv /opt/venv
# Copy package source so the editable install resolves at runtime.
COPY --from=builder /src /src

WORKDIR /app
RUN chown -R kvwarden:kvwarden /app
USER kvwarden

EXPOSE 8000

# /health returns 200 once all engines have warmed up; 503 with a
# {missing_models: [...]} body until then. The healthcheck loop is the
# same one the README quickstart uses.
HEALTHCHECK --interval=10s --timeout=5s --start-period=120s --retries=12 \
    CMD curl -fs http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "kvwarden"]
CMD ["serve", "--config", "/etc/kvwarden/config.yaml"]
