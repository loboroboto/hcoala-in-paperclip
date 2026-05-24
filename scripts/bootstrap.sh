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
#   /data/hermes/              ← Railway persistent volume, MUTABLE
#       memory.db              ← episodic + semantic (FTS5)
#       MEMORY.md              ← curated semantic facts
#       USER.md                ← Honcho-style user model
#       skills/                ← agent-authored skills land here
#       trajectories/          ← exported decision-cycle traces
#
#   ~/.hermes/                 ← Hermes runtime expects this; we SYMLINK
#       AGENTS.md   → /app/hermes-config/AGENTS.md
#       SOUL.md     → /app/hermes-config/SOUL.md
#       hermes.toml → /app/hermes-config/hermes.toml
#       mcp.json    → /app/hermes-config/mcp.json
#       skills/     → /data/hermes/skills (volume-backed, merged with /app)
#       memory.db, MEMORY.md, USER.md, trajectories → /data/hermes/...
#
# This split is the durability story:
#   - Architecture lives in code (foundation = AGENTS.md + SOUL.md + seed skills).
#   - State lives on the volume (episodic memory, learned skills, user model).
#   - A fresh deploy reconstructs ~/.hermes from /app + /data on boot.

set -euo pipefail

CONFIG_DIR="/app/hermes-config"
VOLUME_DIR="/data/hermes"
HERMES_DIR="${HOME:-/root}/.hermes"

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
mkdir -p "$VOLUME_DIR" \
         "$VOLUME_DIR/skills" \
         "$VOLUME_DIR/trajectories" \
         "$VOLUME_DIR/memory"

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
             deploy-railway debug-incident write-quality-code)

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
# 4. Wire ~/.hermes/ via symlinks
# ----------------------------------------------------------------------------
mkdir -p "$HERMES_DIR"

# Architecture files: symlink to git-tracked sources (always current).
ln -sfn "$CONFIG_DIR/AGENTS.md"   "$HERMES_DIR/AGENTS.md"
ln -sfn "$CONFIG_DIR/SOUL.md"     "$HERMES_DIR/SOUL.md"
ln -sfn "$CONFIG_DIR/hermes.toml" "$HERMES_DIR/hermes.toml"
ln -sfn "$CONFIG_DIR/mcp.json"    "$HERMES_DIR/mcp.json"

# State files / dirs: symlink to volume (mutable, persistent).
ln -sfn "$VOLUME_DIR/MEMORY.md"     "$HERMES_DIR/MEMORY.md"
ln -sfn "$VOLUME_DIR/USER.md"       "$HERMES_DIR/USER.md"
ln -sfn "$VOLUME_DIR/skills"        "$HERMES_DIR/skills"
ln -sfn "$VOLUME_DIR/memory.db"     "$HERMES_DIR/memory.db" 2>/dev/null || true
ln -sfn "$VOLUME_DIR/trajectories"  "$HERMES_DIR/trajectories"

log "~/.hermes/ wired:"
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
exec "$@"
