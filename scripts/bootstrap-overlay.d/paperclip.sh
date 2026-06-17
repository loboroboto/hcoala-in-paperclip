#!/usr/bin/env bash
# bootstrap-overlay/paperclip.sh — paperclip-specific boot launches.
#
# Sourced by /app/bootstrap.sh after core setup. Spawns the paperclip-hermes-
# gateway runner (gated on RUNNER_AUTH_TOKEN) and the paperclip-reconcile fleet
# loop (gated on PAPERCLIP_ONBOARD). Both are backgrounded with `&` and inherit
# bootstrap's stdio so logs appear in `railway logs`; tini -g propagates shutdown
# signals to the whole process group.
#
# This file is the paperclip operationalization seam. The upstream snapshot
# of hermes-interprets-coala ships an empty bootstrap-overlay.d/; this script
# is what makes the substrate paperclip-flavored in this deployment.
#
# The script is sourced (not executed), so it inherits bootstrap's
# `set -euo pipefail`, the `log()` function, and every variable bootstrap
# resolved (HERMES_DIR, HERMES_HOME, etc.).

# ----------------------------------------------------------------------------
# Fleet (#8/#10): paperclip-hermes-gateway runner
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
# Fleet (#8): paperclip reconciler (provision → sync → onboard, one process)
# ----------------------------------------------------------------------------
# Auth is the CEO agent's bearer key. Materialize it from PAPERCLIP_CEO_KEY once,
# chmod 600. $HOME is /root here, so this writes /root/.pclip.key (where the
# reconciler looks for it).
if [[ -n "${PAPERCLIP_CEO_KEY:-}" ]] && [[ ! -f "$HOME/.pclip.key" ]]; then
  printf '%s' "$PAPERCLIP_CEO_KEY" > "$HOME/.pclip.key"
  chmod 600 "$HOME/.pclip.key"
  log "wrote $HOME/.pclip.key from PAPERCLIP_CEO_KEY"
fi

# One reconciler replaces the former three boot scripts (provision + sync +
# onboarder). Each loop pass runs all three phases in order:
#   provision — board-key: create + wire the active non-CEO agents (no-op without a
#               board key or any active non-CEO role; the CEO is never imported, #82),
#   sync      — board-key: PUT each active role's AGENTS.md bundle (#82),
#   onboard   — CEO-key, gated on PAPERCLIP_ONBOARD: PATCH the CEO onto hermes_remote →
#               the runner above (clears Paperclip's "Process adapter missing command"
#               error once the board adapter #12 is approved).
# Per-phase gating preserves the former three-script contract: the board key is the
# on-switch for provision+sync, and PAPERCLIP_ONBOARD is the on-switch for onboard. So we
# launch the reconciler when EITHER is present, and each phase self-gates inside (provision
# and sync no-op without a board key; onboard is skipped without PAPERCLIP_ONBOARD).
# Backgrounded (reparents to tini on exec; `tini -g` forwards SIGTERM for clean shutdown)
# and inherits stdio so logs land in `railway logs`. The loop is idempotent + self-healing:
# a Paperclip adapter reset or a dropped non-CEO agent re-converges on the next pass. A
# fatal fault in one phase is isolated to that phase (logged ERROR + surfaced in the
# breadcrumb) and the loop keeps retrying — it never takes the whole reconciler down. A
# latest-pass breadcrumb is written to $HERMES_HOME/reconcile.status (refreshed every pass,
# so its timestamp is a liveness signal). Tunables: PAPERCLIP_ONBOARD_INTERVAL /
# PAPERCLIP_ONBOARD_BACKOFF_MAX.
if [[ -n "${PAPERCLIP_ONBOARD:-}" ]] || [[ -n "${PAPERCLIP_BOARD_KEY:-}" ]] \
   || [[ -f "$HOME/.paperclip/auth.json" ]]; then
  python /app/paperclip-reconcile.py &
  log "started paperclip fleet reconciler loop (pid $!) — provision → sync → onboard"
else
  log "paperclip fleet reconciler disabled (no PAPERCLIP_ONBOARD and no board credential)"
fi
