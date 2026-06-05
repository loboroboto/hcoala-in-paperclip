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
# 2. Ensure volume dirs exist (Railway provides the mount; we create children)
# ----------------------------------------------------------------------------
# The full set hermes expects to find under HERMES_HOME. Missing dirs can
# cause opaque "no such file" errors deep in cron / session / log code paths
# even though the user-facing feature isn't being exercised directly.
mkdir -p "$VOLUME_DIR" \
         "$VOLUME_DIR/skills" \
         "$VOLUME_DIR/trajectories" \
         "$VOLUME_DIR/memory" \
         "$VOLUME_DIR/cron" \
         "$VOLUME_DIR/sessions" \
         "$VOLUME_DIR/logs" \
         "$VOLUME_DIR/pairing" \
         "$VOLUME_DIR/hooks" \
         "$VOLUME_DIR/image_cache" \
         "$VOLUME_DIR/audio_cache" \
         "$VOLUME_DIR/workspace" \
         "$VOLUME_DIR/plans" \
         "$VOLUME_DIR/home"

# Seed runtime state files the admin server (server.py) reads/writes. These
# live on the volume so dashboard-driven edits persist across redeploys.
# The admin server expects both to exist and barfs on missing files.
#   .env          — operator-set secrets and runtime toggles (provider
#                   keys, gateway tokens). hermes.toml's `*_env` indirection
#                   means values landed here flow into the runtime without
#                   touching git-tracked architecture.
#   config.yaml   — hermes runtime config (mcp_servers etc., deep-merged
#                   with user-managed sections on save). Seeded from the
#                   bundled example if absent.
touch "$VOLUME_DIR/.env"
if [[ ! -f "$VOLUME_DIR/config.yaml" ]] && [[ -f /opt/hermes-agent/cli-config.yaml.example ]]; then
  cp /opt/hermes-agent/cli-config.yaml.example "$VOLUME_DIR/config.yaml"
  log "seeded $VOLUME_DIR/config.yaml from cli-config.yaml.example"
fi

# Clear any stale gateway PID file left over from a previous container.
# `hermes gateway` (spawned by the admin server) writes a pid file on
# start but does not always remove it on SIGTERM. Since /data is a
# persistent volume, the file survives container restarts and causes
# every subsequent boot to exit with "PID file race lost". No hermes
# process can be running this early (we're pre-exec in a fresh container),
# so removing unconditionally is safe.
rm -f "$VOLUME_DIR/gateway.pid"

# Bootstrap OAuth tokens from env var. Needed for providers that auth
# via OAuth device flow rather than a static API key (xAI Grok SuperGrok,
# Gemini CLI, Qwen OAuth, Claude Code). Set HERMES_AUTH_JSON_BOOTSTRAP to
# the contents of a locally-generated ~/.hermes/auth.json. Written only
# once — subsequent token refreshes update the file in place on the
# persistent volume.
if [[ ! -f "$VOLUME_DIR/auth.json" ]] && [[ -n "${HERMES_AUTH_JSON_BOOTSTRAP:-}" ]]; then
  printf '%s' "$HERMES_AUTH_JSON_BOOTSTRAP" > "$VOLUME_DIR/auth.json"
  chmod 600 "$VOLUME_DIR/auth.json"
  log "bootstrapped $VOLUME_DIR/auth.json from HERMES_AUTH_JSON_BOOTSTRAP"
fi

# Seed MEMORY.md and USER.md if absent (agent will append to them over time).
if [[ ! -f "$VOLUME_DIR/MEMORY.md" ]]; then
  cat > "$VOLUME_DIR/MEMORY.md" <<'EOF'
# MEMORY.md — Curated Semantic Memory

This file is the agent's hand-curated semantic memory (CoALA §4.1, §4.5).
Stable facts about the user, infrastructure, and codebase, written as
declarative sentences with sources. Updated by the `coala-reflection` skill
and by direct user instruction.

## Format
```
## <topic>
- Claim. (Source: episode <id>, <date>.)
```

## User
_(empty — populated as the agent learns)_

## Infrastructure
_(empty — populated as the agent learns)_

## Codebase
_(empty — populated as the agent learns)_
EOF
  log "seeded $VOLUME_DIR/MEMORY.md"
fi

if [[ ! -f "$VOLUME_DIR/USER.md" ]]; then
  cat > "$VOLUME_DIR/USER.md" <<'EOF'
# USER.md — User Model

Dialectic user model (Honcho-style if enabled, otherwise hand-curated).
What the agent has inferred about the user's preferences, working style,
and goals. Distinct from MEMORY.md: claims here are *about the user*
specifically.

_(empty — populated as the agent learns)_
EOF
  log "seeded $VOLUME_DIR/USER.md"
fi

if [[ ! -f "$VOLUME_DIR/PEERS.md" ]]; then
  cat > "$VOLUME_DIR/PEERS.md" <<'EOF'
# PEERS.md — Peer Agent Model

Semantic memory (CoALA §4.1, §4.5) for other agents the system shares
work with. Parallel to USER.md but for non-human collaborators. See
AGENTS.md §6.

Claims here are about specific peers: their identity, declared
capabilities, observed behavior, trust level, and which channels they
monitor. Updated by direct user instruction, by the `coala-reflection`
skill, and by the `group-agent-coordination` skill when a cycle
produces a durable fact about a peer.

Registered peers in `hermes.toml [[peers.peer]]` are the *declaration*;
this file is the *experience-grounded* model. They drift apart over time
— that's expected. Reconcile during reflection.

## Format
```
## <peer-id>
- Declared capabilities: ...
- Observed behavior: ...
- Trust: untrusted | scoped | trusted
- Channels: <ids of channels where this peer is active>
- Notable episodes: <episode refs or dates>
```

_(empty — populated as the agent collaborates)_
EOF
  log "seeded $VOLUME_DIR/PEERS.md"
fi

# ----------------------------------------------------------------------------
# 3. Seed bundled skills into volume (so they survive even if /app changes)
# ----------------------------------------------------------------------------
# We seed by COPY, not symlink, into the volume — so agent-authored patches
# to seed skills are preserved across rebuilds. The git-tracked originals
# in /app/hermes-config/skills/ remain as the canonical source of truth;
# the bootstrap below honors "volume wins" on conflict (agent edits stick).
#
# To force re-seeding from /app (overwriting volume copies), set
# HERMES_FORCE_RESEED=1 in Railway env.
SEED_SKILLS=(coala-decision-cycle coala-skill-induction coala-reflection
             deploy-railway debug-incident write-quality-code
             group-agent-coordination github-projects-ops channel-aware-messaging)

for skill in "${SEED_SKILLS[@]}"; do
  src="$CONFIG_DIR/skills/$skill"
  dst="$VOLUME_DIR/skills/$skill"

  if [[ ! -d "$src" ]]; then
    log "WARN: seed skill missing in config: $skill"
    continue
  fi

  if [[ ! -d "$dst" ]] || [[ "${HERMES_FORCE_RESEED:-0}" == "1" ]]; then
    rm -rf "$dst"
    cp -r "$src" "$dst"
    log "seeded skill: $skill"
  fi
done

# ----------------------------------------------------------------------------
# 4. Wire HERMES_HOME (the volume) and the ~/.hermes alias
# ----------------------------------------------------------------------------
# All mutable state (state.db, .env, config.yaml, sessions/, logs/, skills/,
# …) is written by hermes/admin *directly* into HERMES_HOME = the volume, so
# there is nothing to symlink for state — it's already persistent. We only
# wire in the read-only, git-tracked architecture.
mkdir -p "$HERMES_DIR"

# Architecture files: symlink to git-tracked sources (always current).
ln -sfn "$CONFIG_DIR/AGENTS.md"   "$HERMES_DIR/AGENTS.md"
ln -sfn "$CONFIG_DIR/SOUL.md"     "$HERMES_DIR/SOUL.md"
ln -sfn "$CONFIG_DIR/hermes.toml" "$HERMES_DIR/hermes.toml"
ln -sfn "$CONFIG_DIR/mcp.json"    "$HERMES_DIR/mcp.json"

# Alias ~/.hermes → the volume. hermes/admin use $HERMES_HOME explicitly, but
# several skill docs and any code path that hardcodes ~/.hermes/... should
# still resolve onto the volume. Skip when HOME already *is* the volume home
# (i.e. HERMES_HOME=~/.hermes) to avoid a self-referential link.
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
# 6. Hand off to the actual command
# ----------------------------------------------------------------------------
# cd into the admin template dir so Jinja2's FileSystemLoader("templates")
# in server.py resolves correctly. Harmless for non-server CMDs.
cd /opt/hermes-admin 2>/dev/null || true
exec "$@"
