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
# 1. Config dir completeness (AGENTS.md/SOUL.md/hermes.toml/mcp.json, git-tracked;
#    missing = misbuild) is validated by seed-hermes-home.sh below — same FATAL
#    checks — so we don't duplicate them here.
# ----------------------------------------------------------------------------

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
# 5b. Bootstrap overlays (downstream extension point)
# ----------------------------------------------------------------------------
# Source any *.sh files in /app/bootstrap-overlay.d/ after core setup but
# before the final exec. Downstream consumers (e.g. paperclip operationalization)
# drop their boot-time launch logic here without forking this script. Overlays
# are sourced — not executed — so they inherit `set -euo pipefail`, the log()
# function, and every variable resolved above. Backgrounded processes spawned
# from an overlay reparent to tini on the final `exec` exactly as inline
# launches did before.
overlay_dir="/app/bootstrap-overlay.d"
if [[ -d "$overlay_dir" ]]; then
  shopt -s nullglob
  for overlay in "$overlay_dir"/*.sh; do
    log "sourcing overlay: $overlay"
    # shellcheck disable=SC1090
    source "$overlay"
  done
  shopt -u nullglob
fi

# ----------------------------------------------------------------------------
# 6. Hand off to the actual command
# ----------------------------------------------------------------------------
# cd into the admin template dir so Jinja2's FileSystemLoader("templates")
# in server.py resolves correctly. Harmless for non-server CMDs.
cd /opt/hermes-admin 2>/dev/null || true
exec "$@"
