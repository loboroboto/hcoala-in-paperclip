#!/usr/bin/env bash
# seed-hermes-home.sh — idempotently provision ONE CoALA-aligned hermes home.
#
# Usage: seed-hermes-home.sh <HOME_DIR>
#
# This is the single source of truth for what a hermes home must contain. It is
# called twice in two contexts:
#
#   1. bootstrap.sh — for the durable MAIN home (HERMES_HOME, the volume root
#      /data/hermes) on every container start.
#   2. hermes-fleet-entry.sh — lazily, for a per-agent fleet home
#      (/data/hermes/agents/<agentId>) the first time the paperclip runner
#      spawns that agent (fleet epic #8, slice #11).
#
# It seeds only the GENERIC, no-clobber pieces that every home needs:
#   - the hermes subdir tree
#   - .env (touched) + config.yaml (from the bundled example, if absent)
#   - MEMORY.md / USER.md / PEERS.md (if absent)
#   - bundled seed skills (copied, so agent edits survive; HERMES_FORCE_RESEED=1
#     overwrites them from /app)
#   - symlinks to the read-only, git-tracked architecture (AGENTS.md, SOUL.md,
#     hermes.toml, mcp.json)
#
# It deliberately does NOT touch the global ~/.hermes alias, gateway.pid, or
# auth.json — those are main-home concerns owned by bootstrap.sh (the fleet
# wrapper handles ~/.hermes per-agent via $HOME instead).
#
# Idempotent: safe to run any number of times. It only writes when something is
# missing or stale and never destroys agent-authored state.

set -euo pipefail

CONFIG_DIR="/app/hermes-config"

HOME_DIR="${1:-}"
if [[ -z "$HOME_DIR" ]]; then
  printf '[seed-home] FATAL: no target home dir given (usage: %s <HOME_DIR>)\n' "$0" >&2
  exit 1
fi

log() { printf '[seed-home] %s\n' "$*" >&2; }

# Config dir is git-tracked; missing = misbuild.
if [[ ! -d "$CONFIG_DIR" ]]; then
  log "FATAL: $CONFIG_DIR not found — Dockerfile didn't copy hermes-config in."
  exit 1
fi
for f in AGENTS.md SOUL.md hermes.toml mcp.json roles/ceo.md; do
  if [[ ! -f "$CONFIG_DIR/$f" ]]; then
    log "FATAL: $CONFIG_DIR/$f missing — config is incomplete."
    exit 1
  fi
done

# ----------------------------------------------------------------------------
# 1. Ensure the hermes subdir tree exists.
# ----------------------------------------------------------------------------
# The full set hermes expects to find under a home. Missing dirs can cause
# opaque "no such file" errors deep in cron / session / log code paths even
# when the user-facing feature isn't being exercised directly.
mkdir -p "$HOME_DIR" \
         "$HOME_DIR/skills" \
         "$HOME_DIR/trajectories" \
         "$HOME_DIR/memory" \
         "$HOME_DIR/cron" \
         "$HOME_DIR/sessions" \
         "$HOME_DIR/logs" \
         "$HOME_DIR/pairing" \
         "$HOME_DIR/hooks" \
         "$HOME_DIR/image_cache" \
         "$HOME_DIR/audio_cache" \
         "$HOME_DIR/workspace" \
         "$HOME_DIR/plans" \
         "$HOME_DIR/onboarding" \
         "$HOME_DIR/home"

# Seed runtime state files hermes/admin read/write. The admin server expects
# both to exist and barfs on missing files.
#   .env          — operator-set secrets and runtime toggles.
#   config.yaml   — hermes runtime config (mcp_servers etc.), seeded from the
#                   bundled example if absent.
touch "$HOME_DIR/.env"
if [[ ! -f "$HOME_DIR/config.yaml" ]] && [[ -f /opt/hermes-agent/cli-config.yaml.example ]]; then
  cp /opt/hermes-agent/cli-config.yaml.example "$HOME_DIR/config.yaml"
  log "seeded $HOME_DIR/config.yaml from cli-config.yaml.example"
fi

# ----------------------------------------------------------------------------
# 2. Seed MEMORY.md / USER.md / PEERS.md if absent (agent appends over time).
# ----------------------------------------------------------------------------
if [[ ! -f "$HOME_DIR/MEMORY.md" ]]; then
  cat > "$HOME_DIR/MEMORY.md" <<'EOF'
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
  log "seeded $HOME_DIR/MEMORY.md"
fi

if [[ ! -f "$HOME_DIR/USER.md" ]]; then
  cat > "$HOME_DIR/USER.md" <<'EOF'
# USER.md — User Model

Dialectic user model (Honcho-style if enabled, otherwise hand-curated).
What the agent has inferred about the user's preferences, working style,
and goals. Distinct from MEMORY.md: claims here are *about the user*
specifically.

_(empty — populated as the agent learns)_
EOF
  log "seeded $HOME_DIR/USER.md"
fi

# Onboarding gate state (fleet #20). Per-agent flag the human-onboarding-handshake
# skill reads/writes; lives in THIS home (never USER.md, which is a shared
# main-home context file) so each fleet agent gates independently. No-clobber: an
# already-onboarded agent keeps humanOnboarded:true across reseeds. agentId is the
# fleet home's basename for /data/hermes/agents/<id>; empty for the main home (the
# skill fills it in on first run).
if [[ ! -f "$HOME_DIR/onboarding/state.json" ]]; then
  case "$HOME_DIR" in
    /data/hermes/agents/*) agent_id="${HOME_DIR##*/}" ;;
    *)                     agent_id="" ;;
  esac
  cat > "$HOME_DIR/onboarding/state.json" <<EOF
{
  "humanOnboarded": false,
  "agentId": "$agent_id",
  "firstContactAt": null,
  "onboardedAt": null,
  "onboardedBy": null,
  "channel": null
}
EOF
  log "seeded $HOME_DIR/onboarding/state.json (humanOnboarded=false)"
fi

if [[ ! -f "$HOME_DIR/PEERS.md" ]]; then
  cat > "$HOME_DIR/PEERS.md" <<'EOF'
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
  log "seeded $HOME_DIR/PEERS.md"
fi

# ----------------------------------------------------------------------------
# 3. Seed bundled skills into the home (copy, so agent edits survive rebuilds).
# ----------------------------------------------------------------------------
# The git-tracked originals in /app/hermes-config/skills/ remain canonical; we
# honor "volume wins" on conflict (agent edits stick). Set HERMES_FORCE_RESEED=1
# to overwrite the home's copies from /app.
SEED_SKILLS=(coala-decision-cycle coala-skill-induction coala-reflection
             deploy-railway debug-incident write-quality-code
             group-agent-coordination github-projects-ops channel-aware-messaging
             human-onboarding-handshake)

for skill in "${SEED_SKILLS[@]}"; do
  src="$CONFIG_DIR/skills/$skill"
  dst="$HOME_DIR/skills/$skill"

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
# 4. Symlink the read-only, git-tracked architecture into the home.
# ----------------------------------------------------------------------------
# All mutable state is written directly into the home; only the architecture is
# linked to the (always-current) git-tracked sources.
ln -sfn "$CONFIG_DIR/AGENTS.md"   "$HOME_DIR/AGENTS.md"
ln -sfn "$CONFIG_DIR/SOUL.md"     "$HOME_DIR/SOUL.md"
ln -sfn "$CONFIG_DIR/hermes.toml" "$HOME_DIR/hermes.toml"
ln -sfn "$CONFIG_DIR/mcp.json"    "$HOME_DIR/mcp.json"
# Role overlays (role-agnostic foundation + per-role gates/duties; the agent reads
# roles/<its-role>.md at runtime — see AGENTS.md §1.1). Whole dir symlinked.
ln -sfn "$CONFIG_DIR/roles"       "$HOME_DIR/roles"
