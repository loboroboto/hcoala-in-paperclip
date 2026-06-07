#!/usr/bin/env python3
"""paperclip-onboarder.py — reconcile fleet/agents.yaml into Paperclip (fleet #8, slice #14).

The control-plane core of the Paperclip fleet. Reads the git-tracked desired-state
registry (fleet/agents.yaml, baked to /app/fleet/agents.yaml in the image), and for
each listed agent computes the desired `hermes_remote` adapter config and PATCHes
Paperclip on drift — onboarding the pre-existing CEO agent onto our runner, which is
what clears Paperclip's `Process adapter missing command` heartbeat error.

Detection = reconcile success: we don't read a board-gated adapter list. We simply
attempt to set adapterType="hermes_remote"; Paperclip rejects it (HTTP 422 on current
master, 400 on older builds, body mentioning "Unknown adapter type") until the adapter
is installed (the single manual board gate, slice #12), then accepts it. So the same
PATCH both onboards and detects.

Modes (slice #15): the default is a **continuous reconcile loop** — it reconciles,
sleeps PAPERCLIP_ONBOARD_INTERVAL, and repeats, so the bridge re-converges after a
Paperclip adapter reset (and onboards once the board adapter, #12, is installed) with no
manual step beyond that one gate. While the adapter is absent it backs off (doubling up
to PAPERCLIP_ONBOARD_BACKOFF_MAX) and logs only on state transitions so it isn't spammy.
`--once` runs a single pass and exits (used by tests/CI). The CEO's *behavior* once
onboarded (it must not act on the company until the human handshake) is slice #20.

Auth: the CEO agent's bearer key (from ~/.pclip.key or $PAPERCLIP_CEO_KEY) suffices for
GET/PATCH /api/agents/{id} — no board session needed (spike #9).

Self-heal boundary: #15 re-onboards when Paperclip's adapter config is reset but the
agent keeps its pinned existingId (GET 200 → re-PATCH). It does NOT self-heal a full
Paperclip DB wipe that re-creates the agent under a new id — the pinned id then 404s and
the loop reports a persistent, non-fatal "absent" status until fleet/agents.yaml is
updated or the agent is auto-created (slice #21). It never auto-installs board creds.

Exit codes (single-pass / loop's per-pass classification):
  0  — reconciled or already in sync (no-op)
  75 — EX_TEMPFAIL: at least one agent is still waiting for the board adapter approval,
       its pinned id is absent, or a CEO id could not be resolved from the key yet
       (all retryable; the loop backs off on this)
  1  — hard error (bad config, missing creds/registry, unexpected I/O)

Config (env):
  FLEET_REGISTRY                 path to agents.yaml (default /app/fleet/agents.yaml)
  PAPERCLIP_API_URL              Paperclip base URL  (default http://paperclip.railway.internal:3100)
  PAPERCLIP_CEO_KEY              CEO bearer key       (fallback if ~/.pclip.key absent)
  PAPERCLIP_ONBOARD_INTERVAL     loop sleep between passes, seconds (default 300)
  PAPERCLIP_ONBOARD_BACKOFF_MAX  back-off cap while waiting/absent, seconds (default 3600)
  <runnerAuthTokenEnv>           runner token, name from registry defaults (default RUNNER_AUTH_TOKEN)
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

DEFAULT_REGISTRY = "/app/fleet/agents.yaml"
DEFAULT_API_URL = "http://paperclip.railway.internal:3100"
DEFAULT_RUNNER_TOKEN_ENV = "RUNNER_AUTH_TOKEN"
PCLIP_KEY_FILE = Path.home() / ".pclip.key"

# Loop tuning (overridable via env; see module docstring).
DEFAULT_ONBOARD_INTERVAL = 300   # base sleep between reconcile passes (s)
DEFAULT_BACKOFF_MAX = 3600       # cap for the doubling back-off while waiting/absent (s)
HEARTBEAT_EVERY_SEC = 3600       # emit a liveness line at least this often when quiet

# Exit codes (see module docstring).
EX_OK = 0
EX_HARD = 1
EX_TEMPFAIL = 75

# Adapter-config keys Paperclip treats as board-only "instructions/bundle configuration":
# an AGENT-key PATCH that ADDS or REMOVES any of these is rejected 403 (server agents.ts
# KNOWN_INSTRUCTIONS_BUNDLE_KEYS). We never send them; we only avoid replaceAdapterConfig
# when the CURRENT config has them, so a replace doesn't count as removing them.
INSTRUCTIONS_BUNDLE_KEYS = (
    "instructionsBundleMode",
    "instructionsRootPath",
    "instructionsEntryFile",
    "instructionsFilePath",
    "agentsMdPath",
)

# Set by the signal handler so a long back-off sleep aborts promptly on SIGTERM/SIGINT.
# A plain bool (assignment is atomic) rather than threading.Event: the handler must do the
# minimum async-signal-safe work — manipulating an Event's lock from a handler that
# interrupted Event.wait() can deadlock. We log + react in the main loop instead.
_stop = False


def log(msg: str) -> None:
    """Stderr logging consistent with the bash scripts ([onboarder] prefix)."""
    print(f"[onboarder] {msg}", file=sys.stderr, flush=True)


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in ≤1s slices so a SIGTERM/SIGINT (which sets _stop) is noticed promptly.
    PEP 475 means time.sleep auto-resumes after a signal rather than raising, so the
    handler runs between slices and we check the flag here."""
    deadline = time.monotonic() + seconds
    while not _stop:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _env_int(name: str, default: int) -> int:
    """Read a positive int from env; warn + fall back to default on bad/empty value."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log(f"WARN: ${name}={raw!r} is not an integer; using {default}")
        return default
    if val <= 0:
        log(f"WARN: ${name}={val} must be > 0; using {default}")
        return default
    return val


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_registry(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        log(f"ERROR: registry not found at {path} (set FLEET_REGISTRY?)")
        sys.exit(EX_HARD)
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        log(f"ERROR: failed to parse {path}: {exc}")
        sys.exit(EX_HARD)
    if not isinstance(data, dict):
        log(f"ERROR: {path} must be a mapping at the top level")
        sys.exit(EX_HARD)
    return data


def load_ceo_key() -> str:
    """CEO bearer key from ~/.pclip.key, else $PAPERCLIP_CEO_KEY."""
    if PCLIP_KEY_FILE.is_file():
        key = PCLIP_KEY_FILE.read_text().strip()
        if key:
            return key
        log(f"WARN: {PCLIP_KEY_FILE} is empty; falling back to PAPERCLIP_CEO_KEY")
    key = os.environ.get("PAPERCLIP_CEO_KEY", "").strip()
    if not key:
        log("ERROR: no CEO key (looked at ~/.pclip.key and $PAPERCLIP_CEO_KEY)")
        sys.exit(EX_HARD)
    return key


# ---------------------------------------------------------------------------
# Desired-state construction
# ---------------------------------------------------------------------------
def build_desired(defaults: dict[str, Any], agent: dict[str, Any],
                  runner_token: str) -> dict[str, Any]:
    """Merge defaults + agent overrides into the target adapterType/adapterConfig/runtimeConfig.

    Maps registry fields onto the hermes_remote adapterConfig shape 1:1 (see
    fleet/README.md). The runner auth token is written as a literal VALUE
    (Paperclip has no {{}} templating).
    """
    merged = {**defaults, **agent}

    adapter_config: dict[str, Any] = {
        "remoteRunnerUrl": merged["remoteRunnerUrl"],
        "runnerAuthToken": runner_token,
        "paperclipApiUrl": merged["paperclipApiUrl"],
        "persistSession": merged.get("persistSession", True),
        "timeoutSec": merged.get("timeoutSec", 600),
    }
    hermes_home = merged.get("hermesHome")
    if hermes_home:
        adapter_config["env"] = {"HERMES_HOME": hermes_home}

    # Model: the adapter passes adapterConfig.model to `hermes chat` as --model,
    # which overrides the per-agent ~/.hermes/config.yaml default (seeded from
    # hermes's upstream cli-config.yaml.example). Without this, fleet agents run
    # whatever that example hardcodes, NOT our chosen model.
    model = merged.get("model")
    if model:
        adapter_config["model"] = model

    hb = merged.get("heartbeat", {}) or {}
    heartbeat = {
        "enabled": hb.get("enabled", True),
        "intervalSec": hb.get("intervalSec", 300),
    }

    return {
        "adapterType": "hermes_remote",
        "adapterConfig": adapter_config,
        "heartbeat": heartbeat,
    }


def needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    """True if any managed field drifts from desired (idempotency check)."""
    if current.get("adapterType") != desired["adapterType"]:
        return True
    cur_cfg = current.get("adapterConfig") or {}
    for k, v in desired["adapterConfig"].items():
        if cur_cfg.get(k) != v:
            return True
    cur_hb = (current.get("runtimeConfig") or {}).get("heartbeat") or {}
    for k, v in desired["heartbeat"].items():
        if cur_hb.get(k) != v:
            return True
    return False


# ---------------------------------------------------------------------------
# Detection helper
# ---------------------------------------------------------------------------
def is_adapter_missing(resp: httpx.Response) -> bool:
    """Detect the "adapter not installed" signal across Paperclip versions.

    Current master → 422 {"error":"Unknown adapter type: hermes_remote"}.
    Older builds   → 400 {"error":"Validation error", ...} (adapterType was an enum).
    Robust signal: status in (400, 422) AND the body mentions an adapter type.
    """
    if resp.status_code not in (400, 422):
        return False
    body = resp.text.lower()
    return "adapter type" in body or "unknown adapter" in body or "hermes_remote" in body


# ---------------------------------------------------------------------------
# CEO resolution (resolve the CEO agent id from the bearer key)
# ---------------------------------------------------------------------------
def resolve_ceo(client: httpx.Client) -> tuple[dict[str, Any] | None, str]:
    """Resolve the CEO agent (id + companyId) the bearer key belongs to, with no
    hardcoded id — so a freshly deployed Paperclip (new instance-generated ids)
    bootstraps from the key alone.

    Strategy (verified against paperclipai/paperclip docs + spike #9, all callable
    with a CEO *agent* key):
      1. GET /api/agents/me → the authenticated agent's record. If role=="ceo" that
         IS the CEO; otherwise the CEO is the chainOfCommand entry with role=="ceo".
      2. Fallback: GET /api/companies/{companyId}/agents and pick role=="ceo".

    Returns ({"id":..., "companyId":...}, message) on success, (None, message) on
    failure. Pure of logging + company-id validation — the caller does both.
    """
    try:
        r = client.get("/api/agents/me")
    except httpx.HTTPError as exc:
        return None, f"resolve-ceo: GET /api/agents/me failed ({exc})"
    if r.status_code != 200:
        return None, f"resolve-ceo: GET /api/agents/me returned {r.status_code} {r.text[:200]}"
    me = r.json()
    company_id = me.get("companyId")

    ceo_id = me.get("id") if me.get("role") == "ceo" else None
    if not ceo_id:
        for entry in me.get("chainOfCommand") or []:
            if entry.get("role") == "ceo":
                ceo_id = entry.get("id")
                break
    if not ceo_id and company_id:
        # Deeper fallback: list the company's agents and find the CEO.
        try:
            lr = client.get(f"/api/companies/{company_id}/agents")
            if lr.status_code == 200:
                for a in lr.json() or []:
                    if a.get("role") == "ceo":
                        ceo_id = a.get("id")
                        break
        except httpx.HTTPError:
            pass  # fall through to the no-CEO error below

    if not ceo_id:
        return None, ("resolve-ceo: no agent with role=ceo found via /me, chainOfCommand, "
                      "or the company agent list")
    return ({"id": ceo_id, "companyId": company_id},
            f"resolve-ceo: CEO resolved to {ceo_id} (company {company_id})")


def _resolve_ceo_cached(client: httpx.Client,
                        cache: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """resolve_ceo memoized for one reconcile pass (the /me lookup is key-global, so a
    single call serves every resolveCeoFromKey company in the pass)."""
    if "ceo" not in cache:
        cache["ceo"] = resolve_ceo(client)
    return cache["ceo"]


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
def reconcile_agent(client: httpx.Client, agent_id: str, desired: dict[str, Any],
                    dry_run: bool) -> tuple[str, str]:
    """Reconcile one agent. Returns (status, message); the CALLER logs the message so
    the loop can suppress repeats. status ∈
    'synced' | 'onboarded' | 'waiting' | 'absent' | 'error'."""
    try:
        get = client.get(f"/api/agents/{agent_id}")
    except httpx.HTTPError as exc:
        return "error", f"{agent_id}: GET failed ({exc})"
    if get.status_code == 404:
        # Pinned existingId no longer exists — a full Paperclip wipe re-created the agent
        # under a new id. Not an error to retry-fix here: updating the id (or creating the
        # agent) is slice #21. Surface it distinctly so it's non-fatal and not log spam.
        return "absent", (f"{agent_id}: agent not found (404) — pinned existingId is stale "
                          f"after a full Paperclip wipe; update fleet/agents.yaml or see #21")
    if get.status_code != 200:
        return "error", f"{agent_id}: GET returned {get.status_code} {get.text[:200]}"
    current = get.json()

    if not needs_update(current, desired):
        return "synced", f"{agent_id}: no changes (already hermes_remote, in sync)"

    # runtimeConfig: read-modify-write so we preserve modelProfiles etc.
    runtime_config = dict(current.get("runtimeConfig") or {})
    runtime_config["heartbeat"] = {
        **(runtime_config.get("heartbeat") or {}),
        **desired["heartbeat"],
    }
    payload: dict[str, Any] = {
        "adapterType": desired["adapterType"],
        "adapterConfig": desired["adapterConfig"],
        "runtimeConfig": runtime_config,
    }

    # Merge mode: replaceAdapterConfig=True gives deterministic drift-restore, BUT Paperclip
    # forbids an AGENT-key caller from REMOVING an agent's instruction-bundle keys
    # (server agents.ts: a replace that drops a KNOWN_INSTRUCTIONS_BUNDLE_KEY → 403
    # "Only board-authenticated callers can manage … bundle configuration"). The CEO agent
    # (claude_local) carries those keys; a plain `general` agent does not. So we replace
    # ONLY when the current config has none of them; otherwise we shallow-merge — our payload
    # omits those keys, and on the adapterType flip Paperclip's preserveInstructionsBundleConfig
    # carries them forward, so the CEO can be onboarded with the key (no board step).
    current_cfg = current.get("adapterConfig") or {}
    has_protected = any(k in current_cfg for k in INSTRUCTIONS_BUNDLE_KEYS)
    if not has_protected:
        payload["replaceAdapterConfig"] = True
    merge_note = "" if not has_protected else " (merge mode — preserving board-protected instruction keys)"

    if dry_run:
        return "synced", (f"{agent_id}: DRY-RUN would PATCH → adapterType=hermes_remote, "
                         f"adapterConfig={_redact(desired['adapterConfig'])}, "
                         f"heartbeat={desired['heartbeat']}{merge_note}")

    try:
        patch = client.patch(f"/api/agents/{agent_id}", json=payload)
    except httpx.HTTPError as exc:
        return "error", f"{agent_id}: PATCH failed ({exc})"

    if patch.status_code == 200:
        return "onboarded", f"{agent_id}: onboarded → hermes_remote{merge_note}"
    if is_adapter_missing(patch):
        return "waiting", (f"{agent_id}: waiting for board adapter approval (adapter not "
                          f"installed) [{patch.status_code}]")
    return "error", f"{agent_id}: PATCH returned {patch.status_code} {patch.text[:200]}"


def _redact(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    if "runnerAuthToken" in out:
        out["runnerAuthToken"] = "***"
    return out


# ---------------------------------------------------------------------------
# Config + pass
# ---------------------------------------------------------------------------
class Config:
    """Resolved, immutable-at-runtime inputs. Loaded once (registry is baked into the
    image, the CEO key is written at boot), so the loop validates them up front and lets
    a genuine config fault be fatal rather than retried forever."""

    def __init__(self, registry_path: str) -> None:
        reg = load_registry(registry_path)
        self.defaults = reg.get("defaults") or {}
        self.companies = reg.get("companies") or []

        token_env = self.defaults.get("runnerAuthTokenEnv", DEFAULT_RUNNER_TOKEN_ENV)
        self.runner_token = os.environ.get(token_env, "").strip()
        if not self.runner_token:
            log(f"ERROR: runner token env ${token_env} is empty; cannot build a valid "
                f"adapterConfig (hermes_remote requires runnerAuthToken)")
            sys.exit(EX_HARD)

        self.ceo_key = load_ceo_key()


def _reconcile_pass(cfg: Config, api_url: str,
                    dry_run: bool) -> list[tuple[str, str, str]]:
    """One reconcile pass. Returns (agent_id, status, message) per managed agent. Pure of
    logging so callers decide what to emit; raises only on truly unexpected errors (the
    loop catches those and backs off)."""
    headers = {"Authorization": f"Bearer {cfg.ceo_key}", "Content-Type": "application/json"}
    # connect fast (5s) so a down Paperclip drops into back-off quickly; allow slow PATCHes.
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

    results: list[tuple[str, str, str]] = []
    ceo_cache: dict[str, Any] = {}  # memoize the per-pass /api/agents/me resolution
    with httpx.Client(base_url=api_url, headers=headers, timeout=timeout) as client:
        for company in cfg.companies:
            cid = company.get("id")
            cid_disp = cid or "?"
            resolve_flag = bool(company.get("resolveCeoFromKey"))
            for agent in company.get("agents") or []:
                name = agent.get("name", "?")
                agent_id = agent.get("existingId")
                note = ""

                # Bootstrap: a CEO agent with no pinned existingId resolves its id from
                # the bearer key, so a fresh instance onboards with no hardcoded ids.
                # existingId, when present, always wins (no surprise live switch).
                if not agent_id and resolve_flag and agent.get("role") == "ceo":
                    resolved, rmsg = _resolve_ceo_cached(client, ceo_cache)
                    if resolved is None:
                        results.append((f"{cid_disp}/{name}", "unresolved", f"{cid_disp}/{name}: {rmsg}"))
                        continue
                    rcompany = resolved.get("companyId")
                    if cid and rcompany and cid != rcompany:
                        results.append((f"{cid_disp}/{name}", "unresolved",
                                        f"{cid_disp}/{name}: registry company {cid} != key's company "
                                        f"{rcompany}; refusing cross-company onboard"))
                        continue
                    agent_id = resolved["id"]
                    note = " (CEO resolved from key)"
                    # No pinned company id (the open-source default) → adopt the resolved one
                    # so logs name the real company instead of "?".
                    if not cid and rcompany:
                        cid_disp = rcompany

                if not agent_id:
                    results.append((f"{cid_disp}/{name}", "skipped",
                                    f"{cid_disp}/{name}: no existingId — skipping "
                                    f"(agent creation is slice #21)"))
                    continue
                desired = build_desired(cfg.defaults, agent, cfg.runner_token)
                status, msg = reconcile_agent(client, agent_id, desired, dry_run)
                results.append((agent_id, status, msg + note))
    return results


def _pass_exit_code(results: list[tuple[str, str, str]]) -> int:
    statuses = [s for _, s, _ in results]
    if "error" in statuses:
        return EX_HARD
    if "waiting" in statuses or "absent" in statuses or "unresolved" in statuses:
        return EX_TEMPFAIL
    return EX_OK


def _summary(results: list[tuple[str, str, str]]) -> str:
    statuses = [s for _, s, _ in results]
    return (f"pass complete: {statuses.count('onboarded')} onboarded, "
            f"{statuses.count('synced')} in-sync, {statuses.count('waiting')} waiting, "
            f"{statuses.count('absent')} absent, {statuses.count('unresolved')} unresolved, "
            f"{statuses.count('error')} error(s)")


def run_once(registry_path: str, api_url: str, dry_run: bool) -> int:
    """Single reconcile pass: log every per-agent line + the summary, return the exit code."""
    cfg = Config(registry_path)
    results = _reconcile_pass(cfg, api_url, dry_run)
    for _, _, msg in results:
        log(msg)
    log(_summary(results))
    return _pass_exit_code(results)


def run_loop(registry_path: str, api_url: str, dry_run: bool) -> int:
    """Continuous reconcile loop: reconcile, sleep, repeat — re-converging after a
    Paperclip adapter reset. Backs off (doubling, capped) while waiting/absent/erroring and
    logs only on state transitions so it isn't spammy. Config is loaded once up front; a
    config fault is fatal (sys.exit propagates), everything else is retried."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = _env_int("PAPERCLIP_ONBOARD_INTERVAL", DEFAULT_ONBOARD_INTERVAL)
    backoff_max = max(_env_int("PAPERCLIP_ONBOARD_BACKOFF_MAX", DEFAULT_BACKOFF_MAX), interval)
    cfg = Config(registry_path)  # fatal config faults exit here, before the loop
    log(f"loop mode: base interval {interval}s, back-off cap {backoff_max}s")

    prev_sig: tuple[str, ...] | None = None
    backoff = interval
    secs_since_log = 0
    while not _stop:
        try:
            results = _reconcile_pass(cfg, api_url, dry_run)
            rc = _pass_exit_code(results)
        except Exception as exc:  # noqa: BLE001 — never let a transient blip kill the loop
            results, rc = [], EX_HARD
            log(f"pass failed unexpectedly ({exc!r}); backing off")

        # Transition-gated logging: emit detail+summary on any change, plus a periodic
        # heartbeat so operators can confirm the loop is alive during a long quiet wait.
        sig = tuple(f"{aid}={st}" for aid, st, _ in results)
        if sig != prev_sig or secs_since_log >= HEARTBEAT_EVERY_SEC:
            for _, _, msg in results:
                log(msg)
            log(_summary(results) if results else "pass produced no results")
            secs_since_log = 0
        prev_sig = sig

        if rc == EX_OK:
            backoff = interval
            sleep_for = interval
        else:
            sleep_for = backoff
            backoff = min(backoff * 2, backoff_max)

        if _stop:
            break
        secs_since_log += sleep_for
        _interruptible_sleep(sleep_for)  # SIGTERM/SIGINT aborts a long back-off within ~1s

    log("shutdown signal received; onboarder loop exited cleanly")
    return EX_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile fleet/agents.yaml into Paperclip.")
    parser.add_argument("--once", action="store_true",
                        help="run a single reconcile pass and exit (used by tests/CI); "
                             "the default is the continuous reconcile loop")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and log the diff without PATCHing (read-only GETs)")
    args = parser.parse_args()

    registry_path = os.environ.get("FLEET_REGISTRY", DEFAULT_REGISTRY)
    api_url = os.environ.get("PAPERCLIP_API_URL", DEFAULT_API_URL).rstrip("/")

    mode = " (dry-run)" if args.dry_run else ""
    if args.once:
        log(f"reconciling {registry_path} → {api_url}{mode} (single pass)")
        return run_once(registry_path, api_url, args.dry_run)
    log(f"reconciling {registry_path} → {api_url}{mode} (loop)")
    return run_loop(registry_path, api_url, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
