#!/usr/bin/env bash
# hermes-fleet-entry.sh — HERMES_CMD wrapper for per-agent fleet runs (#8/#11).
#
# bootstrap.sh launches the paperclip-hermes-gateway runner with
# HERMES_CMD=/app/hermes-fleet-entry.sh, so the runner invokes THIS script
# (instead of `hermes` directly) for every POST /run. The runner forwards the
# adapter's per-agent env (incl. PAPERCLIP_AGENT_ID) but CANNOT override HOME
# (HOME is in the runner's _PRESERVE set), so the only place to give each agent
# its own home + ~/.hermes is right here.
#
# What it does, then hands off to the real hermes:
#   1. Lazily, idempotently provision the agent's home from git-tracked config
#      (first /run for a new agentId seeds it; later runs are a no-op).
#   2. For fleet homes only (/data/hermes/agents/*), re-home the process so
#      ~/.hermes resolves to that agent's own home — full per-agent isolation,
#      no cross-contamination via the global ~/.hermes alias bootstrap set for
#      the main agent.
#
# Home resolution (#11): the adapter's buildPaperclipEnv ALWAYS injects
# PAPERCLIP_AGENT_ID (= the agent uuid) into the run env, independent of
# adapterConfig — that is the RELIABLE per-agent signal. We do NOT depend on
# adapterConfig.env.HERMES_HOME: Paperclip does not reliably persist/return that
# nested value, and the runner's image sets a container default
# HERMES_HOME=/data/hermes, so trusting HERMES_HOME alone silently runs every
# agent in the shared main home (isolation never happens). So:
#   - honor an explicit fleet-path HERMES_HOME if one actually came through, else
#   - derive /data/hermes/agents/<PAPERCLIP_AGENT_ID>, else
#   - fall back to HERMES_HOME (manual/non-Paperclip runs).

set -euo pipefail

if [[ "${HERMES_HOME:-}" == /data/hermes/agents/* ]]; then
  HOME_DIR="$HERMES_HOME"
elif [[ -n "${PAPERCLIP_AGENT_ID:-}" ]]; then
  # Path-safety: agent id is a uuid; reject anything with a slash or empty.
  case "$PAPERCLIP_AGENT_ID" in
    */* | "") echo "[fleet-entry] FATAL: bad PAPERCLIP_AGENT_ID ('$PAPERCLIP_AGENT_ID')" >&2; exit 1 ;;
  esac
  HOME_DIR="/data/hermes/agents/$PAPERCLIP_AGENT_ID"
else
  HOME_DIR="${HERMES_HOME:?HERMES_HOME or PAPERCLIP_AGENT_ID must be set for fleet runs}"
fi

# Re-export so hermes itself resolves its home here — the inherited HERMES_HOME is
# the container default (/data/hermes) when we derive from PAPERCLIP_AGENT_ID.
export HERMES_HOME="$HOME_DIR"

# 1. Idempotent provisioning (single source of truth, shared with bootstrap).
/app/seed-hermes-home.sh "$HOME_DIR"

# 2. Per-agent HOME isolation for fleet homes only.
case "$HOME_DIR" in
  /data/hermes/agents/*)
    # Self-link so $HOME/.hermes == the agent home once HOME points here.
    ln -sfn "$HOME_DIR" "$HOME_DIR/.hermes"
    export HOME="$HOME_DIR"
    ;;
esac

exec hermes "$@"
