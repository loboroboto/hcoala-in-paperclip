# Dockerfile — CoALA-aligned Hermes Agent on Railway
#
# Two-stage layout:
#   1. Builder: install Hermes Agent and its dependencies.
#   2. Runtime: copy in our git-tracked hermes-config + bootstrap, run.
#
# The persistent volume mounts at /data (Railway-configurable). Architecture
# lives in /app (read-only from agent's POV); state lives in /data.

# -----------------------------------------------------------------------------
# Stage 1 — install Hermes
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Hermes Agent. The official install script handles deps; alternately
# install from PyPI / git if you want pinning.
RUN pip install --upgrade pip && \
    pip install hermes-agent

# Pre-warm any heavy deps you know you'll need (avoids first-run latency on Railway).
# RUN pip install pydantic httpx anthropic openai

# -----------------------------------------------------------------------------
# Stage 2 — runtime
# -----------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/root \
    HERMES_CONFIG_DIR=/app/hermes-config \
    HERMES_DATA_DIR=/data/hermes

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        sqlite3 \
        # Optional but commonly needed for shell-tool grounding actions:
        jq \
        openssh-client \
        # Railway CLI for the deploy-railway skill to actually fire:
        && curl -fsSL https://railway.app/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Hermes from builder.
COPY --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy git-tracked architecture: AGENTS.md, SOUL.md, hermes.toml, mcp.json, skills/
WORKDIR /app
COPY hermes-config /app/hermes-config

# Copy bootstrap and make executable.
COPY scripts/bootstrap.sh /app/bootstrap.sh
RUN chmod +x /app/bootstrap.sh

# The /data mount comes from the Railway volume at runtime; don't VOLUME it
# here (Railway manages the mount itself).

# Default entrypoint runs bootstrap, then exec's the CMD.
ENTRYPOINT ["/app/bootstrap.sh"]

# Default command — start Hermes as a long-running service.
# Override in Railway's "Start Command" if you want a different mode
# (e.g. a messaging gateway only, or TUI for debugging via `railway run`).
CMD ["hermes", "serve"]
