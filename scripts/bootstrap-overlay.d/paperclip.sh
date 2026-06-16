#!/usr/bin/env bash
# bootstrap-overlay/paperclip.sh — paperclip-specific boot launches.
#
# Sourced by /app/bootstrap.sh after core setup. Spawns the paperclip-hermes-
# gateway runner (gated on RUNNER_AUTH_TOKEN) and the paperclip-onboarder
# reconcile loop (gated on PAPERCLIP_ONBOARD). Both are backgrounded with `&`
# and inherit bootstrap's stdio so logs appear in `railway logs`; tini -g
# propagates shutdown signals to the whole process group.
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
# Fleet (#8/#14): paperclip onboarder/reconciler
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
# enabled. Runs the continuous reconcile loop (slice #15): it re-converges after a
# Paperclip adapter reset and onboards the CEO once the board adapter (#12) appears,
# backing off while it waits. Tunable via PAPERCLIP_ONBOARD_INTERVAL /
# PAPERCLIP_ONBOARD_BACKOFF_MAX (--once is the test-only single-pass mode). Like the
# runner, background it (reparents to tini on exec) and inherit stdio so its logs land
# in `railway logs`; `tini -g` forwards SIGTERM to it for a clean shutdown.
if [[ -n "${PAPERCLIP_ONBOARD:-}" ]]; then
  python /app/paperclip-onboarder.py &
  log "started paperclip onboarder loop (pid $!)"
else
  log "paperclip onboarder disabled (PAPERCLIP_ONBOARD unset)"
fi

# ----------------------------------------------------------------------------
# Fleet (#8/#48/#56): paperclip company sync (board-key definition plane)
# ----------------------------------------------------------------------------
# Imports the selected company package's active-role AGENTS.md bundle(s) into
# Paperclip's managed instructions bundle, so the CoALA charter + onboarding gate
# reach the CEO's injected prompt natively (mechanism: spike #42, PR #43). The script
# self-gates: PAPERCLIP_COMPANY_TEMPLATE picks the slug (default agentsys-coala until
# #59), and it no-ops when no board credential ($PAPERCLIP_BOARD_KEY or
# ~/.paperclip/auth.json) is present — so the board credential is the effective on/off
# switch. It resolves companyId from the CEO key materialized above, and authorizes the
# import with the board key. Unlike the onboarder loop this is single-shot — the charter
# is git-tracked and deploy-triggered, not runtime-drifting — so run it FOREGROUND and
# let a non-zero exit be non-fatal (boot continues; the next deploy re-runs it).
python /app/paperclip-company-sync.py --once \
  || log "WARN: company sync exited non-zero (boot continues; retries next deploy)"
