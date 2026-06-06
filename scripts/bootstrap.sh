#!/usr/bin/env bash
# bootstrap.sh — idempotent CoALA-aligned Hermes setup.
#
# Runs on every container start (via Dockerfile ENTRYPOINT). Safe to run any
# number of times: it only writes when something is missing or stale, and
# never destroys agent-authored state on the persistent volume.
#
# Layout:
#
#   /app/hermes-config/        ← git-tracked, READ-ONLY in container
#       AGENTS.md
#       SOUL.md
#       hermes.toml
#       mcp.json
#       skills/<seed-skill>/SKILL.md ...
#
#   /data/hermes/              ← Railway persistent volume, MUTABLE; this is
#                                also HERMES_HOME, so hermes writes here directly
#       state.db               ← episodic + semantic session DB (SQLite/FTS5)
#       .env, config.yaml      ← admin-server runtime config + secrets
#       MEMORY.md              ← curated semantic facts
#       USER.md                ← Honcho-style user model
#       PEERS.md               ← peer-agent semantic model (AGENTS.md §6)
#       auth.json              ← OAuth tokens (refreshed in place)
#       skills/                ← agent-authored skills land here
#       trajectories/          ← exported decision-cycle traces
#       sessions/, logs/, pairing/, cron/, hooks/, plans/,
#       image_cache/, audio_cache/, workspace/, home/
#                              ← hermes-native subdirs (created if missing)
#
#   HERMES_HOME = /data/hermes (the volume itself). hermes + the admin
#       server resolve their home from $HERMES_HOME, so state.db, .env,
#       config.yaml, sessions/, logs/, cron/, … are all written *directly*
#       onto the persistent volume — no per-file symlink to keep in sync.
#       Into that home we symlink only the read-only, git-tracked
#       architecture:
#           /data/hermes/AGENTS.md   → /app/hermes-config/AGENTS.md
#           /data/hermes/SOUL.md     → /app/hermes-config/SOUL.md
#           /data/hermes/hermes.toml → /app/hermes-config/hermes.toml
#           /data/hermes/mcp.json    → /app/hermes-config/mcp.json
#
#   ~/.hermes/   → /data/hermes   (single alias). Any code path or skill doc
#       that hardcodes ~/.hermes/... still lands on the volume.
#
# This split is the durability story:
#   - Architecture lives in code (foundation = AGENTS.md + SOUL.md + seed skills).
#   - State lives on the volume (episodic memory, learned skills, user model).
#   - A fresh deploy points HERMES_HOME at the volume and symlinks the
#     architecture in; all mutable state is already on /data, so nothing is
#     lost across redeploys.

set -euo pipefail

CONFIG_DIR="/app/hermes-config"
VOLUME_DIR="/data/hermes"
# HERMES_HOME *is* the volume — hermes writes all state here directly. Honor
# an externally-set HERMES_HOME (the Dockerfile sets it to /data/hermes) so
# this stays in lockstep with the runtime's own home resolution.
HERMES_DIR="${HERMES_HOME:-$VOLUME_DIR}"

log() { printf '[bootstrap] %s\n' "$*" >&2; }

# ----------------------------------------------------------------------------
# 1. Verify config dir (this is git-tracked; missing = misbuild)
# ----------------------------------------------------------------------------
if [[ ! -d "$CONFIG_DIR" ]]; then
  log "FATAL: $CONFIG_DIR not found — Dockerfile didn't copy hermes-config in."
  exit 1
fi

for f in AGENTS.md SOUL.md hermes.toml mcp.json; do
  if [[ ! -f "$CONFIG_DIR/$f" ]]; then
    log "FATAL: $CONFIG_DIR/$f missing — config is incomplete."
    exit 1
  fi
done
log "config dir OK: $CONFIG_DIR"

# ----------------------------------------------------------------------------
# 2. Seed the main hermes home (HERMES_HOME = the volume) + the fleet root
# ----------------------------------------------------------------------------
# All generic, idempotent home provisioning — the hermes subdir tree, .env,
# config.yaml, MEMORY/USER/PEERS, seed skills, and the read-only architecture
# symlinks — lives in the shared helper so the per-agent fleet wrapper
# (hermes-fleet-entry.sh, #11) seeds homes EXACTLY the same way. Railway
# provides the /data mount; the helper creates the children.
/app/seed-hermes-home.sh "$HERMES_DIR"

# Fleet root: per-agent homes (/data/hermes/agents/<agentId>) are lazily seeded
# under here on first /run by hermes-fleet-entry.sh (fleet epic #8, slice #11).
mkdir -p /data/hermes/agents

# Clear any stale gateway PID file left over from a previous container.
# `hermes gateway` (spawned by the admin server) writes a pid file on
# start but does not always remove it on SIGTERM. Since /data is a
# persistent volume, the file survives container restarts and causes
# every subsequent boot to exit with "PID file race lost". No hermes
# process can be running this early (we're pre-exec in a fresh container),
# so removing unconditionally is safe.
rm -f "$HERMES_DIR/gateway.pid"

# Bootstrap OAuth tokens from env var. Needed for providers that auth
# via OAuth device flow rather than a static API key (xAI Grok SuperGrok,
# Gemini CLI, Qwen OAuth, Claude Code). Set HERMES_AUTH_JSON_BOOTSTRAP to
# the contents of a locally-generated ~/.hermes/auth.json. Written only
# once — subsequent token refreshes update the file in place on the
# persistent volume. Main-home only (fleet agents bring their own creds
# via the adapter's per-agent env).
if [[ ! -f "$HERMES_DIR/auth.json" ]] && [[ -n "${HERMES_AUTH_JSON_BOOTSTRAP:-}" ]]; then
  printf '%s' "$HERMES_AUTH_JSON_BOOTSTRAP" > "$HERMES_DIR/auth.json"
  chmod 600 "$HERMES_DIR/auth.json"
  log "bootstrapped $HERMES_DIR/auth.json from HERMES_AUTH_JSON_BOOTSTRAP"
fi

# ----------------------------------------------------------------------------
# 3. Alias ~/.hermes → the main home (main agent only)
# ----------------------------------------------------------------------------
# hermes/admin use $HERMES_HOME explicitly, but several skill docs and any code
# path that hardcodes ~/.hermes/... should still resolve onto the volume. Fleet
# agents get their OWN ~/.hermes via $HOME inside hermes-fleet-entry.sh, so this
# global alias belongs to the main agent only. Skip when HOME already *is* the
# home (HERMES_HOME=~/.hermes) to avoid a self-referential link.
HOME_HERMES="${HOME:-/root}/.hermes"
if [[ "$HOME_HERMES" != "$HERMES_DIR" ]]; then
  # Safe to clobber: pre-exec in a fresh container, the real state lives on
  # the volume this is about to point at.
  rm -rf "$HOME_HERMES"
  ln -sfn "$HERMES_DIR" "$HOME_HERMES"
fi

log "HERMES_HOME wired at $HERMES_DIR (~/.hermes → $HERMES_DIR):"
ls -la "$HERMES_DIR" | sed 's/^/[bootstrap]   /' >&2

# ----------------------------------------------------------------------------
# 5. Sanity check — required env vars
# ----------------------------------------------------------------------------
missing_env=()
[[ -z "${NOUS_API_KEY:-}" ]] && [[ -z "${OPENROUTER_API_KEY:-}" ]] && [[ -z "${OPENAI_API_KEY:-}" ]] \
  && missing_env+=("NOUS_API_KEY (or OPENROUTER_API_KEY / OPENAI_API_KEY)")

if (( ${#missing_env[@]} > 0 )); then
  log "WARN: missing recommended env vars:"
  for v in "${missing_env[@]}"; do log "  - $v"; done
  log "  (agent will run but LLM calls will fail until at least one is set)"
fi

log "bootstrap complete."

# ----------------------------------------------------------------------------
# 5b. Fleet (#8/#10): paperclip-hermes-gateway runner
# ----------------------------------------------------------------------------
# Exposes the hermes_remote endpoint Paperclip calls to spawn `hermes` over the
# Railway private network (GET /health, POST /run with Bearer RUNNER_AUTH_TOKEN).
# Gated on RUNNER_AUTH_TOKEN so the image still boots normally when the fleet
# isn't enabled (the runner exits 1 without a token). We background it and let
# it inherit stdout/stderr so its banner + run logs show up in `railway logs`;
# when we exec the CMD below the runner reparents to tini (PID 1), and `tini -g`
# forwards SIGTERM to the whole group for clean shutdown.
#
# Bind :: — Railway private networking is IPv6; :: is dual-stack on Linux, and
# you can't bind :: and 0.0.0.0 at once.
#
# HERMES_CMD points the runner at our fleet wrapper (hermes-fleet-entry.sh)
# instead of `hermes` directly, so every /run lazily provisions a per-agent home
# from the adapter's HERMES_HOME=/data/hermes/agents/<agentId> and isolates that
# agent's ~/.hermes before exec'ing hermes (slice #11). An externally-set
# HERMES_CMD still wins.
if [[ -n "${RUNNER_AUTH_TOKEN:-}" ]]; then
  if [[ -f /opt/paperclip-runner/runner/server.py ]]; then
    HERMES_CMD="${HERMES_CMD:-/app/hermes-fleet-entry.sh}" \
    RUNNER_HOST="${RUNNER_HOST:-::}" RUNNER_PORT="${RUNNER_PORT:-8788}" \
      python /opt/paperclip-runner/runner/server.py &
    log "started paperclip runner (pid $!) on [${RUNNER_HOST:-::}]:${RUNNER_PORT:-8788} (HERMES_CMD=${HERMES_CMD:-/app/hermes-fleet-entry.sh})"
  else
    log "WARN: RUNNER_AUTH_TOKEN set but /opt/paperclip-runner/runner/server.py missing — runner not started"
  fi
else
  log "paperclip runner disabled (RUNNER_AUTH_TOKEN unset)"
fi

# ----------------------------------------------------------------------------
# 5c. Fleet (#8/#14): paperclip onboarder/reconciler
# ----------------------------------------------------------------------------
# Reconciles the git-tracked fleet/agents.yaml into Paperclip: onboards the
# pre-existing CEO agent onto the hermes_remote adapter (→ our runner above),
# which clears Paperclip's "Process adapter missing command" heartbeat error.
# Detection = reconcile success: the PATCH is rejected until the adapter is
# installed (the single manual board gate, #12), then succeeds — so the same
# call both detects and onboards.
#
# Auth is the CEO agent's bearer key. Mirror the HERMES_AUTH_JSON_BOOTSTRAP
# pattern above: materialize it from PAPERCLIP_CEO_KEY once, chmod 600. $HOME
# is /root here, so this writes /root/.pclip.key (where the onboarder looks).
if [[ -n "${PAPERCLIP_CEO_KEY:-}" ]] && [[ ! -f "$HOME/.pclip.key" ]]; then
  printf '%s' "$PAPERCLIP_CEO_KEY" > "$HOME/.pclip.key"
  chmod 600 "$HOME/.pclip.key"
  log "wrote $HOME/.pclip.key from PAPERCLIP_CEO_KEY"
fi

# Gated on PAPERCLIP_ONBOARD so the image boots normally when the fleet isn't
# enabled. Single pass (--once) for now; the continuous loop is slice #15. Like
# the runner, background it (reparents to tini on exec) and inherit stdio so its
# logs land in `railway logs`.
if [[ -n "${PAPERCLIP_ONBOARD:-}" ]]; then
  python /app/paperclip-onboarder.py --once &
  log "started paperclip onboarder (pid $!)"
else
  log "paperclip onboarder disabled (PAPERCLIP_ONBOARD unset)"
fi

# ----------------------------------------------------------------------------
# 6. Hand off to the actual command
# ----------------------------------------------------------------------------
# cd into the admin template dir so Jinja2's FileSystemLoader("templates")
# in server.py resolves correctly. Harmless for non-server CMDs.
cd /opt/hermes-admin 2>/dev/null || true
exec "$@"
