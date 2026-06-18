#!/usr/bin/env python3
"""paperclip-reconcile.py — the single Paperclip fleet reconciler (fleet #8).

Collapses the former trio — paperclip-company-provision.py, paperclip-company-sync.py,
paperclip-onboarder.py — into one idempotent entrypoint with three ordered phases:

  1. provision — board-key: CREATE the company's active non-CEO agents (POST
     /api/companies/import; the CEO is NEVER imported, #58/#82) and reconcile their
     adapter + role (#1994) + heartbeat. No-op without a board key or active non-CEO roles.
  2. sync      — board-key: PUT each active role's AGENTS.md into its managed
     instructions bundle ({path, content}, #82), so the CoALA charter reaches the prompt.
     No-op without a board key.
  3. onboard   — CEO agent-key: PATCH the CEO onto adapterType=hermes_remote → our runner
     (clears Paperclip's "Process adapter missing command" error). The PATCH is rejected
     until the board adapter is approved (the one manual gate, #12), then accepted — so the
     same call both detects and onboards.

WHY one entrypoint: the three ran sequentially at boot anyway, sharing the same company,
CEO key, companyId, and board key. They were three files only because they shipped
incrementally (#48/#56/#14); the "provision must run before sync" ordering was an implicit
deploy-time contract. Here it is a function-call order, and the shared credential / CEO-
resolution / manifest / client code lives once in paperclip_common.py instead of being
copy-pasted three times.

Modes:
  --loop (default)  reconcile → sleep PAPERCLIP_ONBOARD_INTERVAL → repeat, re-converging
                    after a Paperclip adapter reset and onboarding once #12 lands. Backs off
                    (doubling, capped at PAPERCLIP_ONBOARD_BACKOFF_MAX) while anything is
                    waiting/erroring, and logs only on state transitions (plus a periodic
                    heartbeat) so a long wait for #12 isn't spammy. Wrapping ALL THREE phases
                    in the loop means a Paperclip wipe that drops a non-CEO agent self-heals
                    too, not just the CEO adapter.
  --once            a single pass (tests/CI / manual runs); logs every line.
  --dry-run         compute + log the diff with read-only GETs; no writes.

Per-phase gating (mirrors the former three scripts): provision + sync are board-key phases
that no-op without a board key; onboard runs only when PAPERCLIP_ONBOARD is set. A fatal fault
in one phase (bad manifest, missing key/token) is ISOLATED to that phase — it is recorded as
that phase's hard error and surfaced, but the other phases still run and the loop keeps
retrying; it never takes the whole reconciler down.

Exit code (aggregate = worst phase): 0 ok · 75 EX_TEMPFAIL (retryable: board waiting /
unreachable / id unresolved) · 1 EX_HARD (bad config/creds/manifest, board 401/403,
unexpected response). A latest-pass breadcrumb is written to $RECONCILE_STATUS_FILE
(default $HERMES_HOME/reconcile.status), refreshed every pass so its timestamp is a liveness
signal as well as the outcome.

Config (env): PAPERCLIP_API_URL, PAPERCLIP_COMPANY_TEMPLATE, PAPERCLIP_COMPANIES_BASE,
  FLEET_REGISTRY, PAPERCLIP_CEO_KEY (or ~/.pclip.key), PAPERCLIP_BOARD_KEY (or
  ~/.paperclip/auth.json), RUNNER_AUTH_TOKEN, PAPERCLIP_ONBOARD_INTERVAL,
  PAPERCLIP_ONBOARD_BACKOFF_MAX, RECONCILE_STATUS_FILE, HERMES_HOME. (The runner URL, model,
  and heartbeat now come from fleet/agents.yaml `defaults`, not env — single source.)
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

# The entrypoint is hyphenated (loaded by path in some contexts); put its own directory on
# sys.path so `import paperclip_common` resolves to the sibling module in /app (prod) or
# scripts/ (tests/CI), regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paperclip_common as common  # noqa: E402
from paperclip_common import EX_OK, EX_HARD, EX_TEMPFAIL  # noqa: E402

# --- onboard-phase tuning ---
DEFAULT_RUNNER_TOKEN_ENV = "RUNNER_AUTH_TOKEN"
DEFAULT_ONBOARD_INTERVAL = 300   # base sleep between reconcile passes (s)
DEFAULT_BACKOFF_MAX = 3600       # cap for the doubling back-off while waiting/erroring (s)
HEARTBEAT_EVERY_SEC = 3600       # emit a liveness line at least this often when quiet

# Adapter-config keys Paperclip treats as board-only "instructions/bundle configuration":
# an AGENT-key PATCH that ADDS or REMOVES any → 403 (server KNOWN_INSTRUCTIONS_BUNDLE_KEYS).
# We never send them; we only avoid replaceAdapterConfig when the CURRENT config has them,
# so a replace doesn't count as removing them.
INSTRUCTIONS_BUNDLE_KEYS = (
    "instructionsBundleMode",
    "instructionsRootPath",
    "instructionsEntryFile",
    "instructionsFilePath",
    "agentsMdPath",
)

# --- provision-phase wiring ---
# Our role slugs → Paperclip's role enum (CONFIRMED live 2026-06-17). Import defaults a
# created agent's role to "agent" (#1994); we PATCH it after. The board enum is
# ceo/cto/cmo/cfo/security/engineer/designer/qa/researcher/general — raw slugs are rejected.
PAPERCLIP_ROLE_MAP = {
    "cto": "cto",
    "staff-engineer": "engineer",
    "qa-release-lead": "qa",
    "research-perf-analyst": "researcher",
}

# Set by the signal handler so a long back-off sleep aborts promptly on SIGTERM/SIGINT.
_stop = False
# When non-None, phase loggers append here instead of printing — lets the loop buffer a
# pass and emit it only on a state transition (anti-spam during the long #12 wait), while
# --once leaves it None so every line prints immediately.
_capture: list[str] | None = None


def _emit(line: str) -> None:
    if _capture is None:
        print(line, file=sys.stderr, flush=True)
    else:
        _capture.append(line)


def _phase_log(prefix: str):
    def log(msg: str) -> None:
        _emit(f"[{prefix}] {msg}")
    return log


log_provision = _phase_log("provision")
log_sync = _phase_log("company-sync")
log_onboard = _phase_log("onboarder")
log_main = _phase_log("reconcile")


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in ≤1s slices so a SIGTERM/SIGINT (which sets _stop) is noticed promptly
    (PEP 475 auto-resumes time.sleep after a signal, so the handler runs between slices)."""
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
        log_onboard(f"WARN: ${name}={raw!r} is not an integer; using {default}")
        return default
    if val <= 0:
        log_onboard(f"WARN: ${name}={val} must be > 0; using {default}")
        return default
    return val


# ===========================================================================
# Shared — the hermes_remote adapter target, built from fleet/agents.yaml defaults
# ===========================================================================
def build_adapter_target(defaults: dict[str, Any], overrides: dict[str, Any],
                         runner_token: str) -> dict[str, Any]:
    """Merge the fleet registry `defaults` + per-agent `overrides` into the target
    adapterType/adapterConfig/heartbeat. The SINGLE source of the hermes_remote config for both
    phases: onboard passes the CEO's agent entry as overrides; provision passes {} (defaults
    only). Maps registry fields onto the adapterConfig 1:1; the runner token is a literal VALUE
    (Paperclip has no {{}} templating)."""
    merged = {**defaults, **overrides}
    adapter_config: dict[str, Any] = {
        "remoteRunnerUrl": merged["remoteRunnerUrl"],
        "runnerAuthToken": runner_token,
        "paperclipApiUrl": merged["paperclipApiUrl"],
        "persistSession": merged.get("persistSession", True),
        "timeoutSec": merged.get("timeoutSec", 600),
    }
    model = merged.get("model")
    if model:
        adapter_config["model"] = model
    hb = merged.get("heartbeat", {}) or {}
    return {
        "adapterType": "hermes_remote",
        "adapterConfig": adapter_config,
        "heartbeat": {"enabled": hb.get("enabled", True), "intervalSec": hb.get("intervalSec", 300)},
    }


# ===========================================================================
# PHASE 1 — provision (board-key: create + wire the active non-CEO agents)
# ===========================================================================
def select_provision_roles(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The active non-CEO roles to stand up. `status: active` is the switch (defined-only
    roles are skipped); the CEO is never imported (taken over via per-agent PUT, #82)."""
    return [r for r in manifest.get("roles", [])
            if r.get("name") != common.CEO_ROLE and r.get("status") == "active"]


def collect_provision_files(company_dir: Path, roles: list[dict[str, Any]]) -> dict[str, str]:
    """The inline import payload's files: COMPANY.md + each non-CEO role's AGENTS.md, keyed by
    repo-relative path. Guards the invariant: the CEO file is NEVER included."""
    files = {common.COMPANY_FILE: common.read_company_doc(company_dir, log_provision)}
    for role in roles:
        if role["name"] == common.CEO_ROLE:  # defense-in-depth; selection already excludes it
            log_provision("FATAL: refusing to put the CEO in an import payload")
            sys.exit(EX_HARD)
        files[role["agents_md"]] = common.read_role_bundle(company_dir, role, log_provision)
    return files


def paperclip_role(role_name: str) -> str:
    """Map our role slug to Paperclip's role enum (confirmed by the Phase 0 spike)."""
    return PAPERCLIP_ROLE_MAP.get(role_name, role_name)


def build_import_payload(company_id: str, files: dict[str, str], roles: list[dict[str, Any]],
                         adapter_config: dict[str, Any]) -> dict[str, Any]:
    """The /api/companies/import body. `adapterOverrides` (keyed by role slug) sets adapterType
    + adapterConfig on the created agent; `role` is currently ignored by import (defaults to
    "agent", #1994 — set by the post-import PATCH), kept for forward-compat (#1990)."""
    return {
        "source": {"type": "inline", "files": dict(files)},
        "target": {"mode": "existing_company", "companyId": company_id},
        "adapterOverrides": {
            role["name"]: {
                "adapterType": "hermes_remote",
                "role": paperclip_role(role["name"]),
                "adapterConfig": adapter_config,
            }
            for role in roles
        },
    }


def import_company(client: httpx.Client, payload: dict[str, Any]) -> httpx.Response:
    return client.post("/api/companies/import", json=payload)


def parse_created_agents(resp: httpx.Response) -> dict[str, str]:
    """Extract {slug: agentId} for the agents the import created. Response shape (confirmed
    live): `{"company":{…}, "agents":[{"slug","id","action"}], …}`. {} if unparseable."""
    import json
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    out: dict[str, str] = {}
    for a in (data.get("agents") if isinstance(data, dict) else None) or []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        slug = a.get("slug") or a.get("role") or a.get("name")
        if slug:
            out[slug] = a["id"]
    return out


def agent_needs_update(current: dict[str, Any], adapter_config: dict[str, Any],
                       desired_role: str, heartbeat: dict[str, Any]) -> bool:
    """True if a managed field drifts (mirrors the onboard phase's needs_update, plus role)."""
    if current.get("adapterType") != "hermes_remote":
        return True
    cur_cfg = current.get("adapterConfig") or {}
    for k, v in adapter_config.items():
        if cur_cfg.get(k) != v:
            return True
    if current.get("role") != desired_role:
        return True
    cur_hb = (current.get("runtimeConfig") or {}).get("heartbeat") or {}
    for k, v in heartbeat.items():
        if cur_hb.get(k) != v:
            return True
    return False


def _provision_agent(client: httpx.Client, agent_id: str, adapter_config: dict[str, Any],
                     desired_role: str, heartbeat: dict[str, Any]) -> str:
    """Ensure one agent is hermes_remote + correct adapterConfig + role + heartbeat with the
    BOARD key. PATCH only on drift (no replaceAdapterConfig → merge mode preserves the
    server's managed-bundle keys). Returns 'ok' | 'temp' | 'error'."""
    try:
        get = client.get(f"/api/agents/{agent_id}")
    except httpx.HTTPError as exc:
        log_provision(f"agent {agent_id}: GET failed ({exc})")
        return "temp"
    if common.is_auth_failure(get):
        log_provision(f"agent {agent_id}: GET board auth failed [{get.status_code}] (#42)")
        return "error"
    if get.status_code != 200:
        log_provision(f"agent {agent_id}: GET returned {get.status_code} {get.text[:160]}")
        return "error"
    current = get.json()
    if not agent_needs_update(current, adapter_config, desired_role, heartbeat):
        log_provision(f"agent {agent_id}: in sync (role={desired_role}, hermes_remote, heartbeat)")
        return "ok"
    runtime = dict(current.get("runtimeConfig") or {})
    runtime["heartbeat"] = {**(runtime.get("heartbeat") or {}), **heartbeat}
    payload = {
        "adapterType": "hermes_remote",
        "adapterConfig": adapter_config,   # merge mode — preserves managed-bundle keys
        "role": desired_role,
        "runtimeConfig": runtime,
    }
    try:
        patch = client.patch(f"/api/agents/{agent_id}", json=payload)
    except httpx.HTTPError as exc:
        log_provision(f"agent {agent_id}: PATCH failed ({exc})")
        return "temp"
    if common.is_auth_failure(patch):
        log_provision(f"agent {agent_id}: PATCH board auth failed [{patch.status_code}] (#42)")
        return "error"
    if patch.status_code // 100 == 2:
        log_provision(f"agent {agent_id}: reconciled → role={desired_role}, hermes_remote, heartbeat enabled")
        return "reconciled"
    if patch.status_code // 100 == 5:
        log_provision(f"agent {agent_id}: PATCH server error [{patch.status_code}] {patch.text[:160]}")
        return "temp"
    log_provision(f"agent {agent_id}: PATCH unexpected [{patch.status_code}] {patch.text[:160]}")
    return "error"


def phase_provision(api_url: str, companies_root: str, slug: str, registry_path: str,
                    dry_run: bool, board_key: str | None,
                    transport: httpx.BaseTransport | None = None) -> tuple[int, bool]:
    """Create + wire the active non-CEO agents. No-op (EX_OK) without a board key (real run)
    or when no non-CEO role is active. The CEO is never imported. Returns (exit_code, mutated)
    where `mutated` is True if this pass created or PATCHed an agent (feeds the loop's
    transition signal so a self-heal is logged immediately).

    The adapterConfig + heartbeat baked into each agent come from the fleet registry `defaults`
    (build_adapter_target) — the SAME source the onboard phase uses for the CEO — so specialists
    and the CEO are wired identically."""
    if not dry_run and board_key is None:
        log_provision(f"company {slug}: no board credential — skipping")
        return EX_OK, False

    company_dir = Path(companies_root) / slug
    if not company_dir.is_dir():
        log_provision(f"ERROR: company package not found: {company_dir} "
                      f"(PAPERCLIP_COMPANY_TEMPLATE={slug})")
        return EX_HARD, False

    manifest = common.load_manifest(company_dir, log_provision)
    roles = select_provision_roles(manifest)
    if not roles:
        log_provision(f"company {slug}: no active non-CEO roles — nothing to provision")
        return EX_OK, False
    common.read_company_doc(company_dir, log_provision)        # required by import (#58/#81)
    for role in roles:                                         # fail-closed if a charter is missing
        common.read_role_bundle(company_dir, role, log_provision)
    log_provision(f"company {slug}: {len(roles)} active non-CEO role(s) — "
                  f"{', '.join(r['name'] for r in roles)}")
    defaults = common.load_registry(registry_path, log_provision).get("defaults") or {}
    token_env = defaults.get("runnerAuthTokenEnv", DEFAULT_RUNNER_TOKEN_ENV)

    if dry_run:
        token = os.environ.get(token_env, "").strip() or "<RUNNER_AUTH_TOKEN>"
        target = build_adapter_target(defaults, {}, token)
        adapter_config, heartbeat = target["adapterConfig"], target["heartbeat"]
        company_id = "<resolved-from-CEO-key-at-runtime>"
        existing: dict[str, str] = {}
        ceo_key = common.ceo_key_or_none(log_provision)
        if ceo_key:
            try:
                with common.make_client(api_url, ceo_key, transport) as client:
                    resolved, msg = common.resolve_ceo(client)
                    log_provision(msg)
                    if resolved and resolved.get("companyId"):
                        company_id = resolved["companyId"]
                        existing = common.agent_ids_by_name(client, company_id)
            except httpx.HTTPError as exc:
                log_provision(f"dry-run: ids not resolved ({exc}); using placeholders")
        else:
            log_provision("dry-run: no CEO key; using placeholders")
        absent = [r for r in roles if r["name"] not in existing]
        log_provision(f"DRY-RUN (company {company_id}):")
        if absent:
            files = collect_provision_files(company_dir, absent)
            log_provision(f"  would import (create): {[r['name'] for r in absent]} — "
                          f"files {sorted(files)} (CEO excluded)")
        for role in roles:
            log_provision(f"  would ensure {role['name']}: adapterType=hermes_remote, "
                          f"role={paperclip_role(role['name'])}, heartbeat={heartbeat}")
        return EX_OK, False

    # Real run: token is required to bake a valid adapterConfig (the unified hard error).
    runner_token = common.load_runner_token(token_env, log_provision)
    target = build_adapter_target(defaults, {}, runner_token)
    adapter_config, heartbeat = target["adapterConfig"], target["heartbeat"]
    ceo_key = common.load_ceo_key(log_provision)
    with common.make_client(api_url, ceo_key, transport) as client:
        resolved, msg = common.resolve_ceo(client)
        log_provision(msg)
        if resolved is None:
            return EX_TEMPFAIL, False
        company_id = resolved.get("companyId")
        if not company_id:
            log_provision("resolve-ceo: resolved CEO has no companyId; cannot target the company")
            return EX_TEMPFAIL, False
        existing = common.agent_ids_by_name(client, company_id)

    statuses: list[str] = []
    created_any = False
    with common.make_client(api_url, board_key, transport) as client:
        absent = [r for r in roles if r["name"] not in existing]
        if absent:
            files = collect_provision_files(company_dir, absent)
            payload = build_import_payload(company_id, files, absent, adapter_config)
            log_provision(f"company {slug}: creating {len(absent)} agent(s) — "
                          f"{', '.join(r['name'] for r in absent)}")
            try:
                resp = import_company(client, payload)
            except httpx.HTTPError as exc:
                log_provision(f"import: POST failed ({exc})")
                return EX_TEMPFAIL, False
            if common.is_auth_failure(resp):
                log_provision(f"import: board auth failed [{resp.status_code}] — board key may be expired (#42)")
                return EX_HARD, False
            if resp.status_code // 100 == 5:
                log_provision(f"import: server error [{resp.status_code}] {resp.text[:200]}")
                return EX_TEMPFAIL, False
            if resp.status_code // 100 != 2:
                log_provision(f"import: unexpected [{resp.status_code}] {resp.text[:200]}")
                return EX_HARD, False
            created = parse_created_agents(resp)
            if not created:
                log_provision(f"import: 2xx but no created agent ids parsed — {resp.text[:300]}")
                return EX_HARD, False
            created_any = bool(created)
            existing.update(created)
            for role in absent:
                if role["name"] in created:
                    log_provision(f"role {role['name']}: created {created[role['name']]}")
                else:
                    log_provision(f"role {role['name']}: import returned no id — verify on the board")
                    statuses.append("error")

        for role in roles:
            agent_id = existing.get(role["name"])
            if not agent_id:
                log_provision(f"role {role['name']}: no agent id after provisioning — cannot reconcile")
                statuses.append("error")
                continue
            statuses.append(_provision_agent(client, agent_id, adapter_config,
                                             paperclip_role(role["name"]), heartbeat))

    # `mutated` = this pass created or PATCHed an agent (a self-heal); feeds the loop's
    # transition signature so the event is logged immediately, not buffered until the heartbeat.
    mutated = created_any or "reconciled" in statuses
    if "error" in statuses:
        return EX_HARD, mutated
    if "temp" in statuses:
        return EX_TEMPFAIL, mutated
    log_provision(f"company {slug}: all active non-CEO roles provisioned + reconciled")
    return EX_OK, mutated


# ===========================================================================
# PHASE 2 — sync (board-key: PUT each active role's AGENTS.md bundle, #82)
# ===========================================================================
def select_active_roles(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Roles with status=='active'. defined-only roles are ignored (their agents_md may
    legitimately not exist on disk yet — they activate later)."""
    return [r for r in manifest.get("roles", []) if r.get("status") == "active"]


def collect_definition_files(company_dir: Path, active: list[dict[str, Any]]) -> dict[str, str]:
    """The files PUT to the board: each active role's AGENTS.md, keyed by repo-relative path.
    COMPANY.md is validated separately (read_company_doc) but not pushed (#82)."""
    return {role["agents_md"]: common.read_role_bundle(company_dir, role, log_sync)
            for role in active}


def put_role_bundle(client: httpx.Client, agent_id: str, content: str) -> httpx.Response:
    """Swap an existing agent's managed bundle. Live contract (#58/#82): JSON body with BOTH
    `path` and `content` — raw bytes or a query-only path → 400."""
    return client.put(
        f"/api/agents/{agent_id}/instructions-bundle/file",
        json={"path": common.BUNDLE_FILENAME, "content": content},
    )


def readback_role_bundle(client: httpx.Client, agent_id: str) -> httpx.Response | None:
    """GET the managed bundle file; None on transport error (treated as 'cannot confirm')."""
    try:
        return client.get(
            f"/api/agents/{agent_id}/instructions-bundle/file",
            params={"path": common.BUNDLE_FILENAME},
        )
    except httpx.HTTPError as exc:
        log_sync(f"readback {agent_id}: GET failed ({exc})")
        return None


def _bundle_content(resp: httpx.Response) -> str:
    """Extract the bundle text from a readback. Live board returns {"path":…, "content":…}
    (confirmed in #58); tolerate a raw-text body too."""
    import json
    body = resp.text
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return body
    if isinstance(data, dict) and "content" in data:
        return data["content"]
    return body


def _in_sync(client: httpx.Client, agent_id: str, desired: str) -> bool:
    """True iff the managed bundle already matches desired (trailing-newline tolerant)."""
    rb = readback_role_bundle(client, agent_id)
    if rb is None or rb.status_code != 200:
        return False
    return _bundle_content(rb).rstrip("\n") == desired.rstrip("\n")


def put_active_bundles(client: httpx.Client, drift: list[dict[str, Any]],
                       files: dict[str, str], agent_ids: dict[str, str]) -> int:
    """PUT each drifted active role's AGENTS.md to its agent — the sole write path (#82).
    auth 401/403 → hard; transport/5xx → temp; no resolvable id → hard; 2xx → ok."""
    statuses: list[str] = []
    for role in drift:
        name = role["name"]
        content = files[role["agents_md"]]
        agent_id = agent_ids.get(name)
        if not agent_id:
            # The sync deliberately never creates agents (import did — and duplicated them, #58).
            log_sync(f"role {name}: no agent id resolved — cannot PUT (sync never creates agents)")
            statuses.append("error")
            continue
        try:
            resp = put_role_bundle(client, agent_id, content)
        except httpx.HTTPError as exc:
            log_sync(f"role {name}: PUT failed ({exc})")
            statuses.append("temp")
            continue
        if common.is_auth_failure(resp):
            log_sync(f"role {name}: PUT board auth failed [{resp.status_code}] — board key may be "
                     f"expired (see #42)")
            statuses.append("error")
        elif resp.status_code // 100 == 2:
            log_sync(f"role {name}: bundle written via PUT ({len(content)} bytes)")
            statuses.append("ok")
        elif resp.status_code // 100 == 5:
            log_sync(f"role {name}: PUT server error [{resp.status_code}] {resp.text[:200]}")
            statuses.append("temp")
        else:
            log_sync(f"role {name}: PUT unexpected [{resp.status_code}] {resp.text[:200]}")
            statuses.append("error")
    if "error" in statuses:
        return EX_HARD
    if "temp" in statuses:
        return EX_TEMPFAIL
    return EX_OK


def phase_sync(api_url: str, companies_root: str, slug: str, dry_run: bool,
               board_key: str | None,
               transport: httpx.BaseTransport | None = None) -> tuple[int, bool]:
    """PUT each active role's AGENTS.md bundle. No-op (EX_OK) without a board key (real run).
    Returns (exit_code, mutated) where `mutated` is True if this pass wrote any bundle (a
    drift correction) — feeds the loop's transition signal."""
    if not dry_run and board_key is None:
        log_sync(f"company {slug}: no board credential — skipping")
        return EX_OK, False

    company_dir = Path(companies_root) / slug
    if not company_dir.is_dir():
        log_sync(f"ERROR: company package not found: {company_dir} "
                 f"(PAPERCLIP_COMPANY_TEMPLATE={slug})")
        return EX_HARD, False

    manifest = common.load_manifest(company_dir, log_sync)
    active = select_active_roles(manifest)
    if not active:
        log_sync(f"company {slug}: no active roles in manifest — nothing to sync")
        return EX_OK, False
    common.read_company_doc(company_dir, log_sync)   # packaging validation only — not pushed (#82)
    files = collect_definition_files(company_dir, active)
    log_sync(f"company {slug}: {len(active)} active role(s) — {', '.join(r['name'] for r in active)}")

    if dry_run:
        company_id = "<resolved-from-CEO-key-at-runtime>"
        agent_ids: dict[str, str] = {}
        ceo_key = common.ceo_key_or_none(log_sync)
        if ceo_key:
            try:
                with common.make_client(api_url, ceo_key, transport) as client:
                    resolved, msg = common.resolve_ceo(client)
                    log_sync(msg)
                    if resolved and resolved.get("companyId"):
                        company_id = resolved["companyId"]
                        agent_ids = common.agent_ids_by_role(client, company_id)
                        if resolved.get("id") and "ceo" not in agent_ids:
                            agent_ids["ceo"] = resolved["id"]
            except httpx.HTTPError as exc:
                log_sync(f"dry-run: ids not resolved ({exc}); using placeholders")
        else:
            log_sync("dry-run: no CEO key; using placeholders")
        log_sync(f"DRY-RUN — would PUT per active role (company {company_id}):")
        for role in active:
            aid = agent_ids.get(role["name"], "<unresolved>")
            content = files[role["agents_md"]]
            first = content.splitlines()[0] if content else ""
            log_sync(f"  PUT /api/agents/{aid}/instructions-bundle/file "
                     f"path={common.BUNDLE_FILENAME} ({len(content)} bytes) — {first[:70]}")
        return EX_OK, False

    ceo_key = common.load_ceo_key(log_sync)
    with common.make_client(api_url, ceo_key, transport) as client:
        resolved, msg = common.resolve_ceo(client)
        log_sync(msg)
        if resolved is None:
            return EX_TEMPFAIL, False
        company_id = resolved.get("companyId")
        if not company_id:
            log_sync("resolve-ceo: resolved CEO has no companyId; cannot resolve agents")
            return EX_TEMPFAIL, False
        agent_ids = common.agent_ids_by_role(client, company_id)
    if resolved.get("id") and "ceo" not in agent_ids:
        agent_ids["ceo"] = resolved["id"]

    with common.make_client(api_url, board_key, transport) as client:
        drift: list[dict[str, Any]] = []
        for role in active:
            name = role["name"]
            agent_id = agent_ids.get(name)
            if agent_id and _in_sync(client, agent_id, files[role["agents_md"]]):
                log_sync(f"role {name}: in sync")
            else:
                drift.append(role)
        if not drift:
            log_sync(f"company {slug}: all active roles in sync — no write needed")
            return EX_OK, False
        log_sync(f"company {slug}: {len(drift)} role(s) drifted — writing per-role bundle(s)")
        return put_active_bundles(client, drift, files, agent_ids), True


# ===========================================================================
# PHASE 3 — onboard (CEO agent-key: PATCH onto hermes_remote → our runner)
# ===========================================================================
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


def is_adapter_missing(resp: httpx.Response) -> bool:
    """Detect "adapter not installed" across versions: 422 {"Unknown adapter type…"} on master,
    400 {"Validation error"} on older builds. Signal: status in (400,422) AND body names an
    adapter type."""
    if resp.status_code not in (400, 422):
        return False
    body = resp.text.lower()
    return "adapter type" in body or "unknown adapter" in body or "hermes_remote" in body


def _redact(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    if "runnerAuthToken" in out:
        out["runnerAuthToken"] = "***"
    return out


def _onboard_agent(client: httpx.Client, agent_id: str, desired: dict[str, Any],
                   dry_run: bool) -> tuple[str, str]:
    """Reconcile one agent's adapter plane. Returns (status, message); the caller logs the
    message. status ∈ 'synced'|'onboarded'|'waiting'|'absent'|'error'."""
    try:
        get = client.get(f"/api/agents/{agent_id}")
    except httpx.HTTPError as exc:
        return "error", f"{agent_id}: GET failed ({exc})"
    if get.status_code == 404:
        return "absent", (f"{agent_id}: agent not found (404) — pinned existingId is stale after "
                          f"a full Paperclip wipe; update fleet/agents.yaml or see #21")
    if get.status_code != 200:
        return "error", f"{agent_id}: GET returned {get.status_code} {get.text[:200]}"
    current = get.json()

    if not needs_update(current, desired):
        return "synced", f"{agent_id}: no changes (already hermes_remote, in sync)"

    runtime_config = dict(current.get("runtimeConfig") or {})
    runtime_config["heartbeat"] = {**(runtime_config.get("heartbeat") or {}), **desired["heartbeat"]}
    payload: dict[str, Any] = {
        "adapterType": desired["adapterType"],
        "adapterConfig": desired["adapterConfig"],
        "runtimeConfig": runtime_config,
    }
    # replaceAdapterConfig=True gives deterministic drift-restore, but an AGENT-key caller
    # may not REMOVE an agent's instruction-bundle keys (→ 403). The CEO carries them; a plain
    # agent does not. Replace only when the current config has none; else shallow-merge (our
    # payload omits them, and the adapterType flip carries them forward server-side).
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


class OnboardConfig:
    """Resolved onboard-phase inputs (registry + runner token + CEO key). Built each pass;
    a genuine config fault is fatal (sys.exit) rather than retried forever."""

    def __init__(self, registry_path: str) -> None:
        reg = common.load_registry(registry_path, log_onboard)
        self.defaults = reg.get("defaults") or {}
        self.companies = reg.get("companies") or []
        token_env = self.defaults.get("runnerAuthTokenEnv", DEFAULT_RUNNER_TOKEN_ENV)
        self.runner_token = common.load_runner_token(token_env, log_onboard)
        self.ceo_key = common.load_ceo_key(log_onboard)


def _onboard_pass(cfg: OnboardConfig, api_url: str, dry_run: bool,
                  transport: httpx.BaseTransport | None = None) -> list[tuple[str, str, str]]:
    """One onboard pass. Returns (agent_id, status, message) per managed agent. Pure of
    logging so the caller decides what to emit."""
    results: list[tuple[str, str, str]] = []
    ceo_cache: dict[str, Any] = {}  # memoize the per-pass /api/agents/me resolution
    with common.make_client(api_url, cfg.ceo_key, transport) as client:
        for company in cfg.companies:
            cid = company.get("id")
            cid_disp = cid or "?"
            resolve_flag = bool(company.get("resolveCeoFromKey"))
            for agent in company.get("agents") or []:
                name = agent.get("name", "?")
                agent_id = agent.get("existingId")
                note = ""
                if not agent_id and resolve_flag and agent.get("role") == "ceo":
                    if "ceo" not in ceo_cache:
                        ceo_cache["ceo"] = common.resolve_ceo(client)
                    resolved, rmsg = ceo_cache["ceo"]
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
                    if not cid and rcompany:
                        cid_disp = rcompany
                if not agent_id:
                    results.append((f"{cid_disp}/{name}", "skipped",
                                    f"{cid_disp}/{name}: no existingId — skipping (agent creation is #21)"))
                    continue
                desired = build_adapter_target(cfg.defaults, agent, cfg.runner_token)
                status, msg = _onboard_agent(client, agent_id, desired, dry_run)
                results.append((agent_id, status, msg + note))
    return results


def _pass_exit_code(results: list[tuple[str, str, str]]) -> int:
    statuses = [s for _, s, _ in results]
    if "error" in statuses:
        return EX_HARD
    if "waiting" in statuses or "absent" in statuses or "unresolved" in statuses:
        return EX_TEMPFAIL
    return EX_OK


def _onboard_summary(results: list[tuple[str, str, str]]) -> str:
    statuses = [s for _, s, _ in results]
    return (f"onboard pass: {statuses.count('onboarded')} onboarded, "
            f"{statuses.count('synced')} in-sync, {statuses.count('waiting')} waiting, "
            f"{statuses.count('absent')} absent, {statuses.count('unresolved')} unresolved, "
            f"{statuses.count('error')} error(s)")


def phase_onboard(registry_path: str, api_url: str, dry_run: bool,
                  transport: httpx.BaseTransport | None = None) -> tuple[int, list[tuple[str, str, str]]]:
    """Onboard the CEO (and any pinned agents) onto hermes_remote. Returns (exit_code,
    per-agent results) so the loop can build a transition signature."""
    cfg = OnboardConfig(registry_path)
    results = _onboard_pass(cfg, api_url, dry_run, transport)
    for _, _, msg in results:
        log_onboard(msg)
    log_onboard(_onboard_summary(results))
    return _pass_exit_code(results), results


# ===========================================================================
# Orchestrator — run all three phases, aggregate, surface, breadcrumb
# ===========================================================================
def _worst(*codes: int) -> int:
    """Aggregate exit code: EX_HARD dominates EX_TEMPFAIL dominates EX_OK."""
    if EX_HARD in codes:
        return EX_HARD
    if EX_TEMPFAIL in codes:
        return EX_TEMPFAIL
    return EX_OK


def _status_path() -> Path:
    explicit = os.environ.get("RECONCILE_STATUS_FILE", "").strip()
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("HERMES_HOME", "/data/hermes")) / "reconcile.status"


def write_status(summary: dict[str, int]) -> None:
    """Best-effort latest-pass breadcrumb so the outcome is visible without scraping logs.
    Never fatal. The failure notice prints DIRECTLY to stderr (not through the phase loggers)
    so it is never swallowed by the loop's capture buffer — an unwritable volume is exactly the
    condition you want surfaced."""
    name = {EX_OK: "ok", EX_TEMPFAIL: "tempfail", EX_HARD: "hard-error"}
    try:
        path = _status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        lines = [f"reconcile {name.get(summary['aggregate'], '?')} at {ts}"]
        for phase in ("provision", "sync", "onboard"):
            lines.append(f"  {phase}: {name.get(summary[phase], '?')} ({summary[phase]})")
        path.write_text("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"[reconcile] WARN: could not write status breadcrumb ({exc})",
              file=sys.stderr, flush=True)


def _guard_phase(name: str, thunk, on_fault):
    """Run one phase, converting a fatal SystemExit (bad manifest / missing key / missing token
    raised by the shared loaders) into that phase's hard-error WITHOUT taking down the others or
    the loop. This restores the per-process failure isolation the three boot scripts had: an
    unrelated provision/sync packaging fault must not stop the CEO onboard, and the loop must
    keep retrying. `on_fault` is the value to return for the phase on a fault."""
    try:
        return thunk()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EX_HARD
        log_main(f"{name} phase aborted (exit {code}) — see the lines above; "
                 f"other phases continue and the loop will retry")
        return on_fault


def reconcile_once(api_url: str, companies_root: str, slug: str, registry_path: str,
                   dry_run: bool, transport: httpx.BaseTransport | None = None
                   ) -> tuple[int, tuple, dict[str, int]]:
    """One pass of all three phases. Gating mirrors the former three scripts: provision + sync
    are board-key phases (no-op without a board key), and onboard runs only when PAPERCLIP_ONBOARD
    is set (its historical on-switch). A fatal fault in any phase is isolated to that phase (see
    _guard_phase). Returns (aggregate_rc, transition_signature, per-phase summary)."""
    board_key = common.load_board_key(log_main)
    onboard_enabled = bool(os.environ.get("PAPERCLIP_ONBOARD", "").strip())

    prov_rc, prov_mut = _guard_phase(
        "provision",
        lambda: phase_provision(api_url, companies_root, slug, registry_path, dry_run, board_key, transport),
        (EX_HARD, False))
    sync_rc, sync_mut = _guard_phase(
        "sync",
        lambda: phase_sync(api_url, companies_root, slug, dry_run, board_key, transport),
        (EX_HARD, False))
    if onboard_enabled:
        onboard_rc, onboard_results = _guard_phase(
            "onboard",
            lambda: phase_onboard(registry_path, api_url, dry_run, transport),
            (EX_HARD, []))
    else:
        log_onboard("onboard disabled (PAPERCLIP_ONBOARD unset) — skipping CEO adapter onboard")
        onboard_rc, onboard_results = EX_OK, []

    rc = _worst(prov_rc, sync_rc, onboard_rc)
    summary = {"provision": prov_rc, "sync": sync_rc, "onboard": onboard_rc, "aggregate": rc}
    # The signature drives the loop's transition-gated logging. It carries each board-key phase's
    # (exit code, did-it-mutate) plus the onboard per-agent statuses, so a self-heal (provision
    # created/PATCHed an agent, or sync wrote a bundle) is a detectable transition, not silent.
    sig = (prov_rc, prov_mut, sync_rc, sync_mut,
           tuple(f"{aid}={st}" for aid, st, _ in onboard_results))
    if rc == EX_HARD:
        failed = [p for p in ("provision", "sync", "onboard") if summary[p] == EX_HARD]
        log_main(f"ERROR: reconcile pass had a hard failure in: {', '.join(failed)} "
                 f"(see the per-phase lines above)")
    return rc, sig, summary


def run_once(api_url: str, companies_root: str, slug: str, registry_path: str,
             dry_run: bool, transport: httpx.BaseTransport | None = None) -> int:
    """A single pass: log every line directly, write the breadcrumb, return the exit code."""
    rc, _, summary = reconcile_once(api_url, companies_root, slug, registry_path, dry_run, transport)
    write_status(summary)
    log_main(f"pass complete (aggregate {rc}): provision={summary['provision']}, "
             f"sync={summary['sync']}, onboard={summary['onboard']}")
    return rc


def run_loop(api_url: str, companies_root: str, slug: str, registry_path: str,
             dry_run: bool) -> int:
    """Continuous reconcile loop over all three phases. Backs off (doubling, capped) while
    anything waits/errors and logs the per-pass detail only on a state transition (plus a
    periodic heartbeat) so a long wait for the #12 gate isn't spammy. A fatal fault in one phase
    is isolated to that phase (reconcile_once → _guard_phase) and the loop keeps retrying — it
    never takes the whole reconciler down. The status breadcrumb is refreshed every pass so its
    timestamp doubles as a liveness signal."""
    global _capture
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = _env_int("PAPERCLIP_ONBOARD_INTERVAL", DEFAULT_ONBOARD_INTERVAL)
    backoff_max = max(_env_int("PAPERCLIP_ONBOARD_BACKOFF_MAX", DEFAULT_BACKOFF_MAX), interval)
    log_main(f"loop mode: base interval {interval}s, back-off cap {backoff_max}s "
             f"(phases: provision → sync → onboard)")

    prev_sig: tuple | None = None
    backoff = interval
    secs_since_log = 0
    while not _stop:
        _capture = []
        try:
            rc, sig, summary = reconcile_once(api_url, companies_root, slug, registry_path, dry_run)
        except Exception as exc:  # noqa: BLE001 — never let a transient blip kill the loop
            rc, sig, summary = EX_HARD, ("pass-exception",), \
                {"provision": EX_HARD, "sync": EX_HARD, "onboard": EX_HARD, "aggregate": EX_HARD}
            _capture.append(f"[reconcile] pass failed unexpectedly ({exc!r}); backing off")

        write_status(summary)   # every pass — keeps the breadcrumb timestamp fresh (liveness)
        if sig != prev_sig or secs_since_log >= HEARTBEAT_EVERY_SEC:
            for line in _capture:
                print(line, file=sys.stderr, flush=True)
            secs_since_log = 0
        prev_sig = sig
        _capture = None

        if rc == EX_OK:
            backoff = interval
            sleep_for = interval
        else:
            sleep_for = backoff
            backoff = min(backoff * 2, backoff_max)
        if _stop:
            break
        secs_since_log += sleep_for
        _interruptible_sleep(sleep_for)

    log_main("shutdown signal received; reconcile loop exited cleanly")
    return EX_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile the Paperclip fleet: provision non-CEO agents, sync company "
                    "bundles, and onboard the CEO onto hermes_remote — in one idempotent pass.")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass and exit (tests/CI / manual); default is the loop")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute + log the diff without writing (read-only GETs)")
    args = parser.parse_args()

    api_url = os.environ.get("PAPERCLIP_API_URL", common.DEFAULT_API_URL).rstrip("/")
    companies_root = os.environ.get("PAPERCLIP_COMPANIES_BASE", common.DEFAULT_COMPANIES_BASE)
    registry_path = os.environ.get("FLEET_REGISTRY", common.DEFAULT_REGISTRY)
    slug, is_default = common.resolve_slug()
    if is_default:
        log_main(f"PAPERCLIP_COMPANY_TEMPLATE unset — using default company template {slug!r}")

    mode = " (dry-run)" if args.dry_run else ""
    if args.once:
        log_main(f"reconciling company {slug} → {api_url}{mode} (single pass)")
        return run_once(api_url, companies_root, slug, registry_path, args.dry_run)
    log_main(f"reconciling company {slug} → {api_url}{mode} (loop)")
    return run_loop(api_url, companies_root, slug, registry_path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
