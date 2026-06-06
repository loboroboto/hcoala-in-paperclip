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

Scope (this slice): one idempotent pass. `--once` is the only mode shipped here; the
continuous loop + back-off + self-heal is slice #15. The CEO's *behavior* once
onboarded (it must not act on the company until the human handshake) is slice #20.

Auth: the CEO agent's bearer key (from ~/.pclip.key or $PAPERCLIP_CEO_KEY) suffices for
GET/PATCH /api/agents/{id} — no board session needed (spike #9).

Exit codes:
  0  — reconciled or already in sync (no-op)
  75 — EX_TEMPFAIL: at least one agent is still waiting for the board adapter approval
       (retryable; slice #15's loop backs off on this)
  1  — hard error (bad config, missing creds/registry, unexpected I/O)

Config (env):
  FLEET_REGISTRY          path to agents.yaml      (default /app/fleet/agents.yaml)
  PAPERCLIP_API_URL       Paperclip base URL       (default http://paperclip.railway.internal:3100)
  PAPERCLIP_CEO_KEY       CEO bearer key           (fallback if ~/.pclip.key absent)
  <runnerAuthTokenEnv>    runner token, name from registry defaults (default RUNNER_AUTH_TOKEN)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

DEFAULT_REGISTRY = "/app/fleet/agents.yaml"
DEFAULT_API_URL = "http://paperclip.railway.internal:3100"
DEFAULT_RUNNER_TOKEN_ENV = "RUNNER_AUTH_TOKEN"
PCLIP_KEY_FILE = Path.home() / ".pclip.key"

# Exit codes (see module docstring).
EX_OK = 0
EX_HARD = 1
EX_TEMPFAIL = 75


def log(msg: str) -> None:
    """Stderr logging consistent with the bash scripts ([onboarder] prefix)."""
    print(f"[onboarder] {msg}", file=sys.stderr, flush=True)


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
# Reconcile
# ---------------------------------------------------------------------------
def reconcile_agent(client: httpx.Client, agent_id: str, desired: dict[str, Any],
                    dry_run: bool) -> str:
    """Reconcile one agent. Returns: 'synced' | 'onboarded' | 'waiting' | 'error'."""
    try:
        get = client.get(f"/api/agents/{agent_id}")
    except httpx.HTTPError as exc:
        log(f"{agent_id}: GET failed ({exc})")
        return "error"
    if get.status_code != 200:
        log(f"{agent_id}: GET returned {get.status_code} {get.text[:200]}")
        return "error"
    current = get.json()

    if not needs_update(current, desired):
        log(f"{agent_id}: no changes (already hermes_remote, in sync)")
        return "synced"

    # runtimeConfig: read-modify-write so we preserve modelProfiles etc.
    runtime_config = dict(current.get("runtimeConfig") or {})
    runtime_config["heartbeat"] = {
        **(runtime_config.get("heartbeat") or {}),
        **desired["heartbeat"],
    }
    payload = {
        "adapterType": desired["adapterType"],
        "adapterConfig": desired["adapterConfig"],
        "replaceAdapterConfig": True,  # deterministic drift-restore (default is shallow merge)
        "runtimeConfig": runtime_config,
    }

    if dry_run:
        log(f"{agent_id}: DRY-RUN would PATCH → adapterType=hermes_remote, "
            f"adapterConfig={_redact(desired['adapterConfig'])}, "
            f"heartbeat={desired['heartbeat']}")
        return "synced"

    try:
        patch = client.patch(f"/api/agents/{agent_id}", json=payload)
    except httpx.HTTPError as exc:
        log(f"{agent_id}: PATCH failed ({exc})")
        return "error"

    if patch.status_code == 200:
        log(f"{agent_id}: onboarded → hermes_remote")
        return "onboarded"
    if is_adapter_missing(patch):
        log(f"{agent_id}: waiting for board adapter approval (adapter not installed) "
            f"[{patch.status_code}]")
        return "waiting"
    log(f"{agent_id}: PATCH returned {patch.status_code} {patch.text[:200]}")
    return "error"


def _redact(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    if "runnerAuthToken" in out:
        out["runnerAuthToken"] = "***"
    return out


# ---------------------------------------------------------------------------
# Pass
# ---------------------------------------------------------------------------
def run_once(registry_path: str, api_url: str, dry_run: bool) -> int:
    reg = load_registry(registry_path)
    defaults = reg.get("defaults") or {}
    companies = reg.get("companies") or []

    token_env = defaults.get("runnerAuthTokenEnv", DEFAULT_RUNNER_TOKEN_ENV)
    runner_token = os.environ.get(token_env, "").strip()
    if not runner_token:
        log(f"ERROR: runner token env ${token_env} is empty; cannot build a valid "
            f"adapterConfig (hermes_remote requires runnerAuthToken)")
        return EX_HARD

    ceo_key = load_ceo_key()
    headers = {"Authorization": f"Bearer {ceo_key}", "Content-Type": "application/json"}

    statuses: list[str] = []
    with httpx.Client(base_url=api_url, headers=headers, timeout=30.0) as client:
        for company in companies:
            cid = company.get("id", "?")
            for agent in company.get("agents") or []:
                agent_id = agent.get("existingId")
                name = agent.get("name", "?")
                if not agent_id:
                    log(f"{cid}/{name}: no existingId — skipping (agent creation is slice #21)")
                    continue
                desired = build_desired(defaults, agent, runner_token)
                statuses.append(reconcile_agent(client, agent_id, desired, dry_run))

    onboarded = statuses.count("onboarded")
    synced = statuses.count("synced")
    waiting = statuses.count("waiting")
    errors = statuses.count("error")
    log(f"pass complete: {onboarded} onboarded, {synced} in-sync, "
        f"{waiting} waiting, {errors} error(s)")

    if errors:
        return EX_HARD
    if waiting:
        return EX_TEMPFAIL
    return EX_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile fleet/agents.yaml into Paperclip.")
    parser.add_argument("--once", action="store_true",
                        help="run a single reconcile pass (the only mode this slice ships; "
                             "the continuous loop is slice #15)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and log the diff without PATCHing (read-only GETs)")
    args = parser.parse_args()

    registry_path = os.environ.get("FLEET_REGISTRY", DEFAULT_REGISTRY)
    api_url = os.environ.get("PAPERCLIP_API_URL", DEFAULT_API_URL).rstrip("/")

    if not args.once:
        log("the continuous loop is slice #15; run with --once for a single pass")
        return EX_HARD

    mode = " (dry-run)" if args.dry_run else ""
    log(f"reconciling {registry_path} → {api_url}{mode}")
    return run_once(registry_path, api_url, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
