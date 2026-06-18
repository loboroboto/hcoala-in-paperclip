"""paperclip_common.py — shared layer for the Paperclip fleet reconciler.

Extracted from the former trio (paperclip-onboarder.py / paperclip-company-sync.py /
paperclip-company-provision.py), which each carried a verbatim copy of this credential +
CEO-resolution + manifest + HTTP-client code (the sync's own docstring admitted "Copied
verbatim … a shared module is a larger refactor out of #56's scope"). This is that module;
paperclip-reconcile.py is the single entrypoint that imports it.

Everything here is phase-agnostic: the constants, the two credential planes (CEO agent key
+ board key), CEO resolution from the bearer key, the httpx client seam, company-package
loading, and the slug selector. Functions that log take a `log` callable (build one with
make_log) so each phase keeps its own [prefix]; functions that fail closed raise SystemExit
with EX_HARD, exactly as the originals did.

Underscore-named so it imports as a normal sibling module (the entrypoint is hyphenated and
loaded by path; it puts its own directory on sys.path before importing this).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

# --- Locations / defaults (mirrors of the constants the three scripts each defined) ---------
DEFAULT_API_URL = "http://paperclip.railway.internal:3100"
DEFAULT_COMPANIES_BASE = "/app/companies"
DEFAULT_REGISTRY = "/app/fleet/agents.yaml"
# Fallback company slug when PAPERCLIP_COMPANY_TEMPLATE is unset. agentsys-coala is the only
# packaged company today; replace with the #59 `default-coala` baseline when it lands.
DEFAULT_COMPANY_TEMPLATE = "agentsys-coala"
PCLIP_KEY_FILE = Path.home() / ".pclip.key"
BOARD_AUTH_FILE = Path.home() / ".paperclip" / "auth.json"

# --- Company-package manifest contract ------------------------------------------------------
MANIFEST_NAME = ".paperclip.yaml"
EXPECTED_SCHEMA = "paperclip/v1"
VALID_STATUSES = ("active", "defined-only")
BUNDLE_FILENAME = "AGENTS.md"   # the ?path= value the managed bundle is keyed by
COMPANY_FILE = "COMPANY.md"     # validated as a packaging check; NOT pushed to the board (#82)
CEO_ROLE = "ceo"                # never imported — taken over via per-agent PUT (#82)

# --- Exit codes (shared by every phase + the aggregate) -------------------------------------
EX_OK = 0
EX_HARD = 1
EX_TEMPFAIL = 75

Logger = Callable[[str], None]


def make_log(prefix: str) -> Logger:
    """Build a stderr logger that prints `[prefix] msg` (mirrors the bash scripts' style)."""
    def log(msg: str) -> None:
        print(f"[{prefix}] {msg}", file=sys.stderr, flush=True)
    return log


# ---------------------------------------------------------------------------
# Credentials — two planes: the CEO *agent* key and the *board* key
# ---------------------------------------------------------------------------
def ceo_key_or_none(log: Logger) -> str | None:
    """CEO bearer key from ~/.pclip.key, else $PAPERCLIP_CEO_KEY; None if neither."""
    if PCLIP_KEY_FILE.is_file():
        key = PCLIP_KEY_FILE.read_text().strip()
        if key:
            return key
        log(f"WARN: {PCLIP_KEY_FILE} is empty; falling back to PAPERCLIP_CEO_KEY")
    key = os.environ.get("PAPERCLIP_CEO_KEY", "").strip()
    return key or None


def load_ceo_key(log: Logger) -> str:
    """CEO bearer key, or fail closed (EX_HARD). GET/PATCH /api/agents/{id} is agent-key auth
    (spike #9) — no board session needed to resolve the CEO + its companyId."""
    key = ceo_key_or_none(log)
    if not key:
        log("ERROR: no CEO key (looked at ~/.pclip.key and $PAPERCLIP_CEO_KEY)")
        sys.exit(EX_HARD)
    return key


def _find_board_key(obj: Any) -> str | None:
    """Recursively scan a parsed auth.json for a string value starting with 'pcp_board'.
    The Paperclip CLI's auth.json schema is external/unverified, so tolerate the shape."""
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


def load_board_key(log: Logger) -> str | None:
    """Board key (pcp_board_*) from $PAPERCLIP_BOARD_KEY, else ~/.paperclip/auth.json.
    Returns None when absent — absence is a gate (the definition plane is off), not an error."""
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


def load_runner_token(env_name: str, log: Logger) -> str:
    """The runner auth token (literal VALUE written into adapterConfig.runnerAuthToken).
    Fail closed (EX_HARD) if empty — a hermes_remote adapterConfig is invalid without it, and
    the onboard phase cannot wire the CEO to the runner. This is the SINGLE place the missing
    token is decided (the former split — onboarder fatal vs provisioner warn-and-proceed — is
    gone: both phases now share this hard error)."""
    token = os.environ.get(env_name, "").strip()
    if not token:
        log(f"ERROR: runner token env ${env_name} is empty; cannot build a valid "
            f"adapterConfig (hermes_remote requires runnerAuthToken)")
        sys.exit(EX_HARD)
    return token


# ---------------------------------------------------------------------------
# HTTP — client seam + CEO resolution + agent-list lookups
# ---------------------------------------------------------------------------
def make_client(api_url: str, bearer: str,
                transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The single client-construction seam. Production passes no transport (real network);
    tests pass an httpx.MockTransport. Connect fast (5s) so a down board drops into back-off
    quickly; allow slow PATCHes."""
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
    kwargs: dict[str, Any] = {"base_url": api_url, "headers": headers, "timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def resolve_ceo(client: httpx.Client) -> tuple[dict[str, Any] | None, str]:
    """Resolve the CEO agent (id + companyId) the bearer key belongs to, with no hardcoded id
    — so a freshly deployed Paperclip (new instance-generated ids) bootstraps from the key
    alone. Strategy (all callable with a CEO *agent* key, verified in spike #9):
      1. GET /api/agents/me → if role=="ceo" that IS the CEO; else the chainOfCommand entry
         with role=="ceo".
      2. Fallback: GET /api/companies/{companyId}/agents and pick role=="ceo".
    Returns ({"id":…, "companyId":…}, message) | (None, message). Pure of logging — the caller
    logs the message."""
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
            pass  # fall through to the no-CEO error below

    if not ceo_id:
        return None, ("resolve-ceo: no agent with role=ceo found via /me, chainOfCommand, "
                      "or the company agent list")
    return ({"id": ceo_id, "companyId": company_id},
            f"resolve-ceo: CEO resolved to {ceo_id} (company {company_id})")


def agent_ids_by_role(client: httpx.Client, company_id: str) -> dict[str, str]:
    """Map role→agentId from the company agent list. Returns {} on any failure."""
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


def agent_ids_by_name(client: httpx.Client, company_id: str) -> dict[str, str]:
    """Map agent **name**→agentId from the company agent list. Provisioning keys by name (=
    import slug = manifest role name), which is STABLE across the role PATCH — unlike the
    board role, which is mutated from "agent" to the enum (#1994). Returns {} on any failure."""
    try:
        r = client.get(f"/api/companies/{company_id}/agents")
    except httpx.HTTPError:
        return {}
    if r.status_code != 200:
        return {}
    out: dict[str, str] = {}
    for a in r.json() or []:
        name, aid = a.get("name"), a.get("id")
        if name and aid:
            out[name] = aid
    return out


def is_auth_failure(resp: httpx.Response) -> bool:
    """401/403 → bad/expired board key (see #42). Not retryable; never fall back to the same key."""
    return resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Company-package loading (manifest + role/company files)
# ---------------------------------------------------------------------------
def load_manifest(company_dir: Path, log: Logger) -> dict[str, Any]:
    """Parse + validate companies/<slug>/.paperclip.yaml. Loud EX_HARD on any contract
    violation (the schema string and role shape are a documented contract)."""
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
    schema = data.get("schema")
    if schema != EXPECTED_SCHEMA:
        log(f"ERROR: {p} schema {schema!r} != {EXPECTED_SCHEMA!r}")
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
            log(f"ERROR: {p} role {role['name']!r} has invalid status {status!r} "
                f"(expected one of {VALID_STATUSES})")
            sys.exit(EX_HARD)
        if role["name"] in seen:
            log(f"ERROR: {p} duplicate role {role['name']!r}")
            sys.exit(EX_HARD)
        seen.add(role["name"])
    return data


def read_role_bundle(company_dir: Path, role: dict[str, Any], log: Logger) -> str:
    """Read a role's agents_md. Path-traversal guarded; fail-closed (EX_HARD) if the file is
    missing or empty — a role being stood up with no operating frame is a packaging bug."""
    rel = role["agents_md"]
    base = company_dir.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        log(f"ERROR: role {role['name']!r} agents_md {rel!r} escapes {base}")
        sys.exit(EX_HARD)
    if not target.is_file():
        log(f"ERROR: role {role['name']!r} bundle missing: {target}")
        sys.exit(EX_HARD)
    content = target.read_text()
    if not content.strip():
        log(f"ERROR: role {role['name']!r} bundle is empty: {target}")
        sys.exit(EX_HARD)
    return content


def read_company_doc(company_dir: Path, log: Logger) -> str:
    """Validate the package's COMPANY.md (definition-plane charter, git-tracked source of
    truth): a valid package ships a non-empty COMPANY.md. It is required by /api/companies/import
    (#58/#81) but NOT written by the per-agent PUT — only AGENTS.md reaches the agent prompt
    (#82). Fail-closed (EX_HARD) if absent or empty. Returns the content."""
    p = company_dir / COMPANY_FILE
    if not p.is_file():
        log(f"ERROR: {p} missing — a valid company package requires {COMPANY_FILE} "
            f"(also required by /api/companies/import, #58/#81)")
        sys.exit(EX_HARD)
    content = p.read_text()
    if not content.strip():
        log(f"ERROR: {p} is empty")
        sys.exit(EX_HARD)
    return content


def resolve_slug() -> tuple[str, bool]:
    """Company slug from PAPERCLIP_COMPANY_TEMPLATE, else DEFAULT_COMPANY_TEMPLATE.
    Returns (slug, is_default)."""
    explicit = os.environ.get("PAPERCLIP_COMPANY_TEMPLATE", "").strip()
    if explicit:
        return explicit, False
    return DEFAULT_COMPANY_TEMPLATE, True


def load_registry(path: str, log: Logger) -> dict[str, Any]:
    """Parse the fleet desired-state registry (fleet/agents.yaml). Its `defaults` block is the
    single source of the hermes_remote adapterConfig for BOTH the onboard phase (the CEO) and
    the provision phase (the specialists). Fail closed (EX_HARD) on missing/unparseable."""
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
