# Dockerfile — CoALA-aligned Hermes Agent on Railway
#
# Single-stage layout (uv-based):
#   - Architecture lives in /app/hermes-config (read-only from agent's POV).
#   - State lives in /data (Railway-mounted persistent volume).
#   - Bootstrap reconstructs ~/.hermes/ from /app + /data on every boot.
#
# Operational lessons borrowed from praveen-ks-2001/hermes-agent-template:
#   - tini as PID 1 to reap zombies (long-uptime containers with MCP stdio
#     servers, browser daemons, etc. otherwise pile up zombies and eventually
#     hit the kernel PID-table limit → "fork: cannot allocate memory").
#   - Pinned HERMES_REF for reproducibility instead of unpinned pip install.
#   - Pre-build the hermes ui-tui bundle so `hermes --tui` (referenced in our
#     README's interactive-debug section) doesn't freeze 30-60s on first
#     connection doing npm install.
#   - uv-based image for faster Python installs.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    HERMES_CONFIG_DIR=/app/hermes-config \
    HERMES_DATA_DIR=/data/hermes

# Which hermes-agent revision to install. Accepts any git ref the upstream
# repo publishes — a release tag (recommended for reproducibility) or a
# branch name (`main`) for bleeding edge.
#
# To bump: check https://github.com/NousResearch/hermes-agent/releases for
# the newest tag (format `vYYYY.M.D`) and update the default below. Use
# `main` only if you accept that every rebuild can pull arbitrary new
# upstream commits.
ARG HERMES_REF=v2026.5.16

# tini = tiny init that runs as PID 1. Without it, hermes's grandchild
# processes (MCP stdio servers, git, bun, browser daemons spawned by tools)
# reparent to PID 1 when their parents exit and pile up as zombies. After
# weeks of uptime that exhausts the kernel's PID table → "fork: cannot
# allocate memory" and the container dies. tini reaps zombies in the
# background and forwards SIGTERM/SIGINT to our entrypoint so Railway's
# stop signal still triggers our graceful shutdown.
#
# Node.js is required only at build time to compile the Hermes ui-tui
# bundle. We strip the source + apt lists afterwards to keep the image
# lean.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tini \
        git \
        curl \
        ca-certificates \
        sqlite3 \
        jq \
        openssh-client && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    curl -fsSL https://railway.app/install.sh | sh && \
    rm -rf /var/lib/apt/lists/*

# Install hermes-agent (provides the `hermes` CLI) at pinned ref. The [all]
# extras in current hermes pull cron, cli, dev, pty, mcp, homeassistant,
# sms, acp, google, web, youtube. Heavy backends (messaging gateways, TTS,
# Honcho, Bedrock/Anthropic, edge-tts, hindsight) are pre-installed via
# explicit extras so first-message latency is instant and the agent can
# opt in to any of them at config time without a rebuild.
#
# When bumping HERMES_REF, re-check hermes-agent's pyproject.toml [all]
# and the extras list against the new release.
RUN git clone --depth 1 --branch ${HERMES_REF} \
        https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent && \
    cd /opt/hermes-agent && \
    uv pip install --system --no-cache -e ".[all,messaging,tts-premium,honcho,bedrock,anthropic,edge-tts,hindsight]" && \
    cd /opt/hermes-agent/ui-tui && \
    npm install --silent --no-fund --no-audit --progress=false && \
    npm run build && \
    rm -rf /opt/hermes-agent/web /opt/hermes-agent/.git /root/.npm

# Points hermes at our pre-built TUI bundle. hermes's _make_tui_argv
# checks HERMES_TUI_DIR first: if dist/entry.js exists there, it skips
# the npm install/build entirely. Pre-building at image time avoids the
# 30-60s npm bootstrap that git-editable installs would otherwise trigger
# on first /chat connection.
ENV HERMES_TUI_DIR=/opt/hermes-agent/ui-tui

# Copy git-tracked architecture: AGENTS.md, SOUL.md, hermes.toml, mcp.json, skills/
WORKDIR /app
COPY hermes-config /app/hermes-config

# Copy bootstrap and make executable.
COPY scripts/bootstrap.sh /app/bootstrap.sh
RUN chmod +x /app/bootstrap.sh

# The /data mount comes from the Railway volume at runtime; don't VOLUME
# it here (Railway manages the mount itself). For local `docker run`, pass
# `-v <name>:/data` to materialize the same shape.

# tini wraps our bootstrap so it runs as PID 1's child. `-g` propagates
# signals to the whole process group so `docker stop` / Railway's SIGTERM
# cleanly terminates the entire tree, not just bootstrap.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/bootstrap.sh"]

# Default command — start Hermes as a long-running service. Override in
# Railway's "Start Command" if you want a different mode (e.g. a messaging
# gateway only, or TUI for debugging via `railway run`).
CMD ["hermes", "serve"]
