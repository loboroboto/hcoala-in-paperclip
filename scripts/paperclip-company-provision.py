#!/usr/bin/env python3
"""paperclip-company-provision.py — board-key provisioner for NON-CEO roles (fleet #8, epic #48).

The third board-key script in the fleet trio. Where the onboarder syncs the *adapter* plane
(agent-key PATCH) and the company-sync (#82) syncs the *definition* plane of agents that already
exist (per-agent PUT), this script **creates** the company's non-CEO agents from the package so
they can then be wired (onboarder) and bundled (sync). It is the "template-started" half of
provisioning; CEO-driven hiring (#21) is the other half, and both converge on the same
`agentId → onboarder → sync` tail.

Hard invariant: **the CEO is never imported.** An agent holding a board/CEO key "takes over" the
CEO via per-agent PUT (#82); imports carry only non-CEO roles, so the existing-company duplicate
bug that #58 hit (import duplicates an *existing* role's agent) is structurally impossible here.

Mechanism (single pass; --once accepted for parity):
  1. Parse companies/<slug>/.paperclip.yaml; select the **non-CEO** roles; read each one's
     agents_md + the package COMPANY.md off disk (fail-closed if missing/empty). COMPANY.md is
     required by /api/companies/import (proven in #58/#81).
  2. Resolve companyId + the existing role→agentId map via the CEO key. Skip any non-CEO role
     that already has a board agent (idempotent — never re-import an existing agent).
  3. For the roles that need creating, POST /api/companies/import with target
     {mode:existing_company, companyId}, files = {COMPANY.md, each non-CEO agents/<role>/AGENTS.md}
     (the CEO file is never included), and adapterOverrides that wire each new agent to the shared
     hermes runner. Capture the created agentIds, then PATCH each agent's role (import drops
     operational defaults incl. role → defaults to "agent"; see paperclipai/paperclip #1994).
  4. Emit the new {role: agentId} block for fleet/agents.yaml. The operator then runs the
     onboarder (adapter wire) + flips the role's manifest status to active (the #82 sync bundles
     it). Activation order matters: provision → wire → activate.

Import contract — CONFIRMED live 2026-06-17 (Phase 0 spike): `adapterOverrides` (keyed by role
slug) sets adapterType + adapterConfig on the created agent but NOT its role — import defaults
role to "agent" (paperclipai/paperclip #1994), so the post-import role PATCH is required. The
import result is `{company, agents:[{slug,id,action}]}`; the valid role enum includes
ceo/cto/cmo/cfo/security/engineer/designer/qa/researcher/general (PAPERCLIP_ROLE_MAP targets are
all valid; our raw slugs are rejected). new_company import + agent/company DELETE return 200.

Exit codes (mirror the sync/onboarder):
  0  — provisioned / nothing to do / disabled (no-op) / dry-run
  75 — EX_TEMPFAIL: board unreachable, 5xx, or companyId not resolvable yet (retryable)
  1  — hard error (missing CEO key, bad/missing manifest, role file missing, board 401/403,
       unexpected import response)

Config (env): PAPERCLIP_COMPANY_TEMPLATE, PAPERCLIP_BOARD_KEY, PAPERCLIP_CEO_KEY,
  PAPERCLIP_API_URL, PAPERCLIP_COMPANIES_BASE, plus the runner wiring the import bakes in:
  PAPERCLIP_RUNNER_URL (default the shared runner), RUNNER_AUTH_TOKEN, PAPERCLIP_FLEET_MODEL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

# --- Constants (mirror paperclip-company-sync.py; self-contained per the trio's convention) ---
DEFAULT_API_URL = "http://paperclip.railway.internal:3100"
DEFAULT_COMPANIES_BASE = "/app/companies"
DEFAULT_COMPANY_TEMPLATE = "agentsys-coala"
PCLIP_KEY_FILE = Path.home() / ".pclip.key"
BOARD_AUTH_FILE = Path.home() / ".paperclip" / "auth.json"

MANIFEST_NAME = ".paperclip.yaml"
EXPECTED_SCHEMA = "paperclip/v1"
VALID_STATUSES = ("active", "defined-only")
BUNDLE_FILENAME = "AGENTS.md"
COMPANY_FILE = "COMPANY.md"
CEO_ROLE = "ceo"  # never imported — the invariant this whole script is built around

# Shared-runner wiring the import bakes into each new agent's adapter (mirrors the onboarder's
# build_desired adapterConfig; the onboarder re-converges it on every deploy regardless).
DEFAULT_RUNNER_URL = "http://hermes-interprets-coala.railway.internal:8788/run"
DEFAULT_PAPERCLIP_INTERNAL = "http://paperclip.railway.internal:3100"

# Our role slugs → Paperclip's role enum (CONFIRMED live 2026-06-17). Import defaults a created
# agent's role to "agent" (#1994); we PATCH it afterward. The board enum is
# ceo/cto/cmo/cfo/security/engineer/designer/qa/researcher/general — our raw slugs
# (staff-engineer, qa-release-lead, research-perf-analyst) are rejected (invalid_enum_value), so
# these mappings are required.
PAPERCLIP_ROLE_MAP = {
    "cto": "cto",
    "staff-engineer": "engineer",
    "qa-release-lead": "qa",
    "research-perf-analyst": "researcher",
}

EX_OK = 0
EX_HARD = 1
EX_TEMPFAIL = 75


def log(msg: str) -> None:
    """Stderr logging consistent with the bash scripts ([company-provision] prefix)."""
    print(f"[company-provision] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Credentials (mirror paperclip-company-sync.py)
# ---------------------------------------------------------------------------
def _ceo_key_or_none() -> str | None:
    if PCLIP_KEY_FILE.is_file():
        key = PCLIP_KEY_FILE.read_text().strip()
        if key:
            return key
        log(f"WARN: {PCLIP_KEY_FILE} is empty; falling back to PAPERCLIP_CEO_KEY")
    key = os.environ.get("PAPERCLIP_CEO_KEY", "").strip()
    return key or None


def load_ceo_key() -> str:
    key = _ceo_key_or_none()
    if not key:
        log("ERROR: no CEO key (looked at ~/.pclip.key and $PAPERCLIP_CEO_KEY)")
        sys.exit(EX_HARD)
    return key


def _find_board_key(obj: Any) -> str | None:
    if isinstance(obj, str):
        return obj if obj.startswith("pcp_board") else None
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_board_key(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_board_key(v)
            if found:
                return found
    return None


def load_board_key() -> str | None:
    key = os.environ.get("PAPERCLIP_BOARD_KEY", "").strip()
    if key:
        return key
    if BOARD_AUTH_FILE.is_file():
        try:
            data = json.loads(BOARD_AUTH_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log(f"WARN: could not read/parse {BOARD_AUTH_FILE} ({exc}); ignoring")
            return None
        found = _find_board_key(data)
        if found:
            return found
        log(f"WARN: {BOARD_AUTH_FILE} has no pcp_board_* value")
    return None


# ---------------------------------------------------------------------------
# Company package loading (mirror paperclip-company-sync.py)
# ---------------------------------------------------------------------------
def load_manifest(company_dir: Path) -> dict[str, Any]:
    p = company_dir / MANIFEST_NAME
    if not p.is_file():
        log(f"ERROR: manifest not found at {p}")
        sys.exit(EX_HARD)
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        log(f"ERROR: failed to parse {p}: {exc}")
        sys.exit(EX_HARD)
    if not isinstance(data, dict):
        log(f"ERROR: {p} must be a mapping at the top level")
        sys.exit(EX_HARD)
    if data.get("schema") != EXPECTED_SCHEMA:
        log(f"ERROR: {p} schema {data.get('schema')!r} != {EXPECTED_SCHEMA!r}")
        sys.exit(EX_HARD)
    roles = data.get("roles")
    if not isinstance(roles, list):
        log(f"ERROR: {p} 'roles' must be a list")
        sys.exit(EX_HARD)
    seen: set[str] = set()
    for role in roles:
        if not isinstance(role, dict) or not role.get("name") or not role.get("agents_md"):
            log(f"ERROR: {p} each role needs 'name' and 'agents_md': {role!r}")
            sys.exit(EX_HARD)
        status = role.get("status", "defined-only")
        if status not in VALID_STATUSES:
            log(f"ERROR: {p} role {role['name']!r} has invalid status {status!r}")
            sys.exit(EX_HARD)
        if role["name"] in seen:
            log(f"ERROR: {p} duplicate role {role['name']!r}")
            sys.exit(EX_HARD)
        seen.add(role["name"])
    return data


def select_provision_roles(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The roles this script may create: every role EXCEPT the CEO. The CEO is never imported
    (it is taken over via per-agent PUT, #82) — excluding it makes a duplicate CEO impossible."""
    return [r for r in manifest.get("roles", []) if r.get("name") != CEO_ROLE]


def read_role_bundle(company_dir: Path, role: dict[str, Any]) -> str:
    rel = role["agents_md"]
    base = company_dir.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        log(f"ERROR: role {role['name']!r} agents_md {rel!r} escapes {base}")
        sys.exit(EX_HARD)
    if not target.is_file():
        log(f"ERROR: role {role['name']!r} bundle missing: {target} — cannot provision a role "
            f"with no charter")
        sys.exit(EX_HARD)
    content = target.read_text()
    if not content.strip():
        log(f"ERROR: role {role['name']!r} bundle is empty: {target}")
        sys.exit(EX_HARD)
    return content


def read_company_doc(company_dir: Path) -> str:
    """COMPANY.md is required by /api/companies/import (422 without it, #58/#81)."""
    p = company_dir / COMPANY_FILE
    if not p.is_file():
        log(f"ERROR: {p} missing — /api/companies/import requires {COMPANY_FILE}")
        sys.exit(EX_HARD)
    content = p.read_text()
    if not content.strip():
        log(f"ERROR: {p} is empty")
        sys.exit(EX_HARD)
    return content


def collect_provision_files(company_dir: Path, roles: list[dict[str, Any]]) -> dict[str, str]:
    """The inline import payload's files: COMPANY.md + each non-CEO role's AGENTS.md, keyed by
    repo-relative path. Guards the invariant: the CEO file is NEVER included."""
    files = {COMPANY_FILE: read_company_doc(company_dir)}
    for role in roles:
        if role["name"] == CEO_ROLE:  # defense-in-depth; select_provision_roles already excludes
            log("FATAL: refusing to put the CEO in an import payload")
            sys.exit(EX_HARD)
        files[role["agents_md"]] = read_role_bundle(company_dir, role)
    return files


def paperclip_role(role_name: str) -> str:
    """Map our role slug to Paperclip's role enum. SEAM: confirmed by the Phase 0 spike."""
    return PAPERCLIP_ROLE_MAP.get(role_name, role_name)


# ---------------------------------------------------------------------------
# HTTP — client seam + board operations (mirror paperclip-company-sync.py)
# ---------------------------------------------------------------------------
def make_client(api_url: str, bearer: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
    kwargs: dict[str, Any] = {"base_url": api_url, "headers": headers, "timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def resolve_ceo(client: httpx.Client) -> tuple[dict[str, Any] | None, str]:
    """Resolve the CEO agent (id + companyId) from the bearer key. Mirrors the sync/onboarder."""
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
        try:
            lr = client.get(f"/api/companies/{company_id}/agents")
            if lr.status_code == 200:
                for a in lr.json() or []:
                    if a.get("role") == "ceo":
                        ceo_id = a.get("id")
                        break
        except httpx.HTTPError:
            pass
    if not ceo_id:
        return None, "resolve-ceo: no agent with role=ceo found"
    return {"id": ceo_id, "companyId": company_id}, f"resolve-ceo: CEO {ceo_id} (company {company_id})"


def resolve_agent_ids(client: httpx.Client, company_id: str) -> dict[str, str]:
    """Map role→agentId from the company agent list (for idempotency: skip roles that exist)."""
    try:
        r = client.get(f"/api/companies/{company_id}/agents")
    except httpx.HTTPError:
        return {}
    if r.status_code != 200:
        return {}
    out: dict[str, str] = {}
    for a in r.json() or []:
        role, aid = a.get("role"), a.get("id")
        if role and aid:
            out[role] = aid
    return out


def is_auth_failure(resp: httpx.Response) -> bool:
    return resp.status_code in (401, 403)


def build_adapter_config() -> dict[str, Any]:
    """The hermes_remote adapterConfig the import bakes into each new agent (mirrors the
    onboarder's build_desired). The onboarder re-converges this on the next deploy regardless,
    so a missing token here is a warning, not fatal."""
    cfg: dict[str, Any] = {
        "remoteRunnerUrl": os.environ.get("PAPERCLIP_RUNNER_URL", DEFAULT_RUNNER_URL),
        "paperclipApiUrl": os.environ.get("PAPERCLIP_API_URL", DEFAULT_PAPERCLIP_INTERNAL),
        "persistSession": True,
        "timeoutSec": 600,
    }
    token = os.environ.get("RUNNER_AUTH_TOKEN", "").strip()
    if token:
        cfg["runnerAuthToken"] = token
    model = os.environ.get("PAPERCLIP_FLEET_MODEL", "").strip()
    if model:
        cfg["model"] = model
    return cfg


def build_import_payload(company_id: str, files: dict[str, str],
                         roles: list[dict[str, Any]], adapter_config: dict[str, Any]) -> dict[str, Any]:
    """The /api/companies/import request body. CONFIRMED live (2026-06-17): `adapterOverrides`
    keyed by role slug sets adapterType + adapterConfig on the created agent; the `role` field
    here is IGNORED by import (role defaults to "agent", #1994) — kept for forward-compat when
    #1990 lands; today the role is set by the post-import PATCH."""
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
    """Extract {slug: agentId} for the agents the import created. Response shape CONFIRMED live
    (2026-06-17): `{"company":{...}, "agents":[{"slug","id","action"}], ...}`. Returns {} if
    unparseable."""
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


def patch_agent_role(client: httpx.Client, agent_id: str, role: str) -> httpx.Response:
    """Set a created agent's role. CONFIRMED live (2026-06-17): import leaves role="agent"
    (#1994); PATCH /api/agents/{id} {role} with the board key sets it (the onboarder handles the
    adapter). Drop once import honors the override (#1990)."""
    return client.patch(f"/api/agents/{agent_id}", json={"role": role})


# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------
def provision_once(api_url: str, companies_root: str, slug: str, dry_run: bool,
                   transport: httpx.BaseTransport | None = None) -> int:
    company_dir = Path(companies_root) / slug
    if not company_dir.is_dir():
        log(f"ERROR: company package not found: {company_dir} (PAPERCLIP_COMPANY_TEMPLATE={slug})")
        return EX_HARD

    manifest = load_manifest(company_dir)
    roles = select_provision_roles(manifest)
    if not roles:
        log(f"company {slug}: no non-CEO roles in manifest — nothing to provision")
        return EX_OK
    read_company_doc(company_dir)  # required by import (#58/#81)
    bundles = {role["name"]: read_role_bundle(company_dir, role) for role in roles}
    log(f"company {slug}: {len(roles)} non-CEO role(s) — {', '.join(r['name'] for r in roles)}")

    def to_provision(existing: dict[str, str]) -> list[dict[str, Any]]:
        out = []
        for role in roles:
            if role["name"] in existing:
                log(f"role {role['name']}: agent already exists ({existing[role['name']]}) — skip")
            else:
                out.append(role)
        return out

    adapter_config = build_adapter_config()

    # Dry-run: resolve ids best-effort, log the import it would send + the role PATCHes. No writes.
    if dry_run:
        company_id = "<resolved-from-CEO-key-at-runtime>"
        existing: dict[str, str] = {}
        ceo_key = _ceo_key_or_none()
        if ceo_key:
            try:
                with make_client(api_url, ceo_key, transport) as client:
                    resolved, msg = resolve_ceo(client)
                    log(msg)
                    if resolved and resolved.get("companyId"):
                        company_id = resolved["companyId"]
                        existing = resolve_agent_ids(client, company_id)
            except httpx.HTTPError as exc:
                log(f"dry-run: ids not resolved ({exc}); using placeholders")
        else:
            log("dry-run: no CEO key; using placeholders")
        pending = to_provision(existing)
        if not pending:
            log("DRY-RUN — all non-CEO roles already provisioned; nothing to import")
            return EX_OK
        files = {COMPANY_FILE: read_company_doc(company_dir)}
        files.update({r["agents_md"]: bundles[r["name"]] for r in pending})
        payload = build_import_payload(company_id, files, pending, adapter_config)
        log(f"DRY-RUN — would POST /api/companies/import (company {company_id}):")
        log(f"  files: {sorted(payload['source']['files'])}  (CEO excluded)")
        for r in pending:
            log(f"  create role {r['name']} → adapterType=hermes_remote role={paperclip_role(r['name'])}; "
                f"then PATCH role={paperclip_role(r['name'])}")
        return EX_OK

    # Real run: resolve companyId + existing agents via the CEO key.
    ceo_key = load_ceo_key()
    with make_client(api_url, ceo_key, transport) as client:
        resolved, msg = resolve_ceo(client)
        log(msg)
        if resolved is None:
            return EX_TEMPFAIL
        company_id = resolved.get("companyId")
        if not company_id:
            log("resolve-ceo: resolved CEO has no companyId; cannot target import")
            return EX_TEMPFAIL
        existing = resolve_agent_ids(client, company_id)

    pending = to_provision(existing)
    if not pending:
        log(f"company {slug}: all non-CEO roles already provisioned — nothing to do")
        return EX_OK

    board_key = load_board_key()
    if board_key is None:  # main gates this, but stay self-contained
        log("no board credential available; skipping")
        return EX_OK
    if "runnerAuthToken" not in adapter_config:
        log("WARN: RUNNER_AUTH_TOKEN unset — imported adapters carry no token; the onboarder "
            "re-converges the adapter on the next deploy, so this is non-fatal")

    files = {COMPANY_FILE: read_company_doc(company_dir)}
    files.update({r["agents_md"]: bundles[r["name"]] for r in pending})
    payload = build_import_payload(company_id, files, pending, adapter_config)
    log(f"company {slug}: importing {len(pending)} non-CEO role(s) — {', '.join(r['name'] for r in pending)}")

    with make_client(api_url, board_key, transport) as client:
        try:
            resp = import_company(client, payload)
        except httpx.HTTPError as exc:
            log(f"import: POST failed ({exc})")
            return EX_TEMPFAIL
        if is_auth_failure(resp):
            log(f"import: board auth failed [{resp.status_code}] — board key may be expired (#42)")
            return EX_HARD
        if resp.status_code // 100 == 5:
            log(f"import: server error [{resp.status_code}] {resp.text[:200]}")
            return EX_TEMPFAIL
        if resp.status_code // 100 != 2:
            log(f"import: unexpected [{resp.status_code}] {resp.text[:200]}")
            return EX_HARD

        created = parse_created_agents(resp)
        if not created:
            log(f"import: 2xx but no created agent ids parsed from response — {resp.text[:300]}")
            return EX_HARD  # the spike confirms the response shape; fail loud until then

        # Fix each new agent's role (import defaults it to "agent", #1994). Best-effort: the
        # agent exists either way; a failed role PATCH is a warning the operator/onboarder fixes.
        rc = EX_OK
        for role in pending:
            aid = created.get(role["name"])
            if not aid:
                log(f"role {role['name']}: import returned no id — verify on the board")
                rc = EX_HARD
                continue
            try:
                pr = patch_agent_role(client, aid, paperclip_role(role["name"]))
                if pr.status_code // 100 == 2:
                    log(f"role {role['name']}: created {aid}, role set to {paperclip_role(role['name'])}")
                else:
                    log(f"role {role['name']}: created {aid} but role PATCH [{pr.status_code}] "
                        f"{pr.text[:160]} — set it manually")
            except httpx.HTTPError as exc:
                log(f"role {role['name']}: created {aid} but role PATCH failed ({exc})")

        log("provisioned — add these to fleet/agents.yaml under the company's agents:")
        for role in pending:
            aid = created.get(role["name"], "<missing>")
            log(f"  - name: {role['name']}")
            log(f"    role: {paperclip_role(role['name'])}")
            log(f"    existingId: {aid}")
        log("then: run the onboarder (adapter wire) + flip these roles' manifest status to "
            "active (the #82 sync bundles them).")
        return rc


def resolve_slug() -> tuple[str, bool]:
    explicit = os.environ.get("PAPERCLIP_COMPANY_TEMPLATE", "").strip()
    if explicit:
        return explicit, False
    return DEFAULT_COMPANY_TEMPLATE, True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision (create) the selected company package's NON-CEO agents on the "
                    "Paperclip board (board-key import; the CEO is never imported).")
    parser.add_argument("--once", action="store_true",
                        help="run a single provision pass and exit (accepted for parity)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log the import + role PATCHes without writing (read-only)")
    args = parser.parse_args()

    api_url = os.environ.get("PAPERCLIP_API_URL", DEFAULT_API_URL).rstrip("/")
    companies_root = os.environ.get("PAPERCLIP_COMPANIES_BASE", DEFAULT_COMPANIES_BASE)
    slug, is_default = resolve_slug()
    if is_default:
        log(f"PAPERCLIP_COMPANY_TEMPLATE unset — using default company template {slug!r}")

    if not args.dry_run and load_board_key() is None:
        log(f"company {slug}: no board credential "
            f"($PAPERCLIP_BOARD_KEY or ~/.paperclip/auth.json) — skipping")
        return EX_OK

    mode = " (dry-run)" if args.dry_run else ""
    log(f"provisioning non-CEO agents for company {slug} → {api_url}{mode}")
    return provision_once(api_url, companies_root, slug, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
