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

# Git-tracked architecture dir (must match seed-hermes-home.sh's CONFIG_DIR). Used as
# the pure CoALA base when composing SOUL.md in step 4.
CONFIG_DIR="/app/hermes-config"

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

# 2b. Gated-provisional guard (fleet #38). A role with an active activation gate
# (e.g. the CEO) must re-run its gate on EVERY run — but persistSession makes the
# adapter replay --resume and resume a session that may "remember" being onboarded,
# defeating the gate. So while this agent is gated AND not yet onboarded, force a
# fresh session (drop --resume) so the gate re-fires from a clean slate. Keyed on
# onboarding/state.json fields the gate skill maintains: gateActive=true (set ONLY
# by a gated role) + humanOnboarded!=true. Non-gated agents (no gateActive) keep
# normal continuity; onboarded agents resume normally.
force_fresh=0
state_file="$HOME_DIR/onboarding/state.json"
if [[ -f "$state_file" ]]; then
  force_fresh=$(python - "$state_file" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    s = json.load(open(sys.argv[1]))
    print(1 if (s.get("gateActive") is True and s.get("humanOnboarded") is not True) else 0)
except Exception:
    print(0)
PY
)
fi
[[ "$force_fresh" == "1" ]] && \
  printf '[fleet-entry] gated+provisional — forcing a fresh session (ignoring --resume) so the activation gate re-fires\n' >&2

# 3. Guard the resume session id. The hermes_remote adapter persists the parsed
# session id and replays it as `--resume <id>`; its fallback parser can capture a
# garbage id (notably the literal "from", from hermes's own "Use a session ID from
# a previous CLI run" error text), and an id from a prior/other home won't exist
# here either. Resuming a non-existent session hard-fails the whole run. So only
# keep a --resume/-r whose session file actually exists in THIS home; otherwise
# drop it and let hermes start fresh (which then persists a real id — self-healing).
# Wording below avoids "session id"/"session saved" so it can't feed the adapter's
# legacy regex if this run later errors.
args=(); i=1
while (( i <= $# )); do
  cur="${!i}"
  if [[ "$cur" == "--resume" || "$cur" == "-r" ]] && (( i < $# )); then
    nxt=$((i + 1)); sid="${!nxt}"
    if [[ "$force_fresh" != "1" && -f "$HOME_DIR/sessions/session_$sid.json" ]]; then
      args+=("$cur" "$sid")
    else
      printf '[fleet-entry] dropping --resume %q (stale, or gated+provisional); starting fresh\n' "$sid" >&2
    fi
    i=$((i + 2)); continue
  fi
  args+=("$cur"); i=$((i + 1))
done

# 4. Relocate Paperclip company directives from the -q user prompt into SOUL.md
# (system/identity context). Option #2 / Route R: when this agent has a Paperclip-
# managed instructions bundle (adapterConfig.instructionsFilePath, set from the
# company definition's agents/<role>/AGENTS.md), the hermes_remote adapter reads that
# file SERVER-SIDE and PREPENDS it to the query as
#   "<company AGENTS.md>\n\n---\n\n<wake prompt>"
# That would land the company charter/gate in the USER message. Hermes loads SOUL.md
# from HERMES_HOME as the first/identity section of the system prompt (stable tier,
# cwd-independent; hermes.toml [context] does NOT apply to `hermes chat`), so we move
# the company block there and strip it from -q. seed-hermes-home.sh re-points SOUL.md
# at the git-tracked CoALA base on every run (just above), so we always recompose from
# the pure base = CoALA identity + the current company directives (board edits to the
# bundle flow through on the next run). Split on the LAST separator so a company doc
# that itself contains a "---" rule isn't truncated (the short wake prompt won't carry
# the full "\n\n---\n\n" sequence).
qidx=-1
for ((j = 0; j < ${#args[@]}; j++)); do
  if [[ "${args[j]}" == "-q" || "${args[j]}" == "--query" ]]; then qidx=$((j + 1)); break; fi
done
if (( qidx >= 0 && qidx < ${#args[@]} )); then
  qtmp="$(mktemp)"; printf '%s' "${args[qidx]}" > "$qtmp"
  wake="$(HERMES_HOME="$HOME_DIR" COALA_SOUL="$CONFIG_DIR/SOUL.md" python3 - "$qtmp" <<'PY'
import os, sys
SEP = "\n\n---\n\n"
prompt = open(sys.argv[1], encoding="utf-8").read()
idx = prompt.rfind(SEP)
if idx == -1:               # no company directive prepended — pass the prompt through
    sys.stdout.write(prompt); sys.exit(0)
company = prompt[:idx].strip()
wake = prompt[idx + len(SEP):]
home = os.environ["HERMES_HOME"]
base = ""
for p in (os.environ.get("COALA_SOUL", ""), os.path.join(home, "SOUL.md")):
    try:
        base = open(p, encoding="utf-8").read(); break
    except Exception:
        pass
soul = (base.rstrip()
        + "\n\n---\n\n# Company Charter & Operating Directives\n\n"
        + "_Injected from the Paperclip company definition (instructions bundle); "
        + "governs as system/identity context._\n\n" + company + "\n")
tmp = os.path.join(home, "SOUL.md.fleet-tmp")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(soul)
os.replace(tmp, os.path.join(home, "SOUL.md"))   # replaces the seed symlink with a real file
sys.stdout.write(wake)
PY
)"
  rc=$?
  rm -f "$qtmp"
  if [[ $rc -eq 0 ]]; then
    args[qidx]="$wake"
    printf '[fleet-entry] relocated company directives into SOUL.md (system context); -q now carries only the wake prompt\n' >&2
  else
    printf '[fleet-entry] WARN: company-directive relocation failed (rc=%s); leaving -q unchanged\n' "$rc" >&2
  fi
fi

exec hermes "${args[@]}"
