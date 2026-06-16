#!/usr/bin/env python3
"""paperclip-company-sync.py — board-key definition-plane sync (fleet #8, epic #48, slice S8/#56).

The board-key sibling of paperclip-onboarder.py. Where the onboarder syncs the *adapter*
plane (agent-key PATCH of adapterType/adapterConfig), this script syncs the *definition*
plane: it reads the selected company package off the image and imports its **active**-role
`AGENTS.md` bundle(s) into Paperclip's managed instructions bundle — so the CoALA charter +
onboarding gate reach the CEO's injected prompt natively (the mechanism spike #42 proved and
PR #43 productized).

Selection + gating:
  - PAPERCLIP_COMPANY_TEMPLATE names the company slug under /app/companies/<slug>/. Unset →
    falls back to DEFAULT_COMPANY_TEMPLATE (agentsys-coala for now, until #59 ships default-coala).
  - A board credential gates whether a real run may proceed (the effective on/off switch).
    Absent (and not --dry-run) → no-op.
    Board key (pcp_board_*, instance-admin, expires; spike #42) from $PAPERCLIP_BOARD_KEY, else
    a tolerant scan of ~/.paperclip/auth.json. Board ops are board-key-gated: an agent key 403s
    on the instructions-bundle keys, so this is a distinct credential from the onboarder's.

Two credentials: the board key authorizes the import write; the existing CEO *agent* key
(~/.pclip.key / $PAPERCLIP_CEO_KEY) resolves the live companyId via resolve_ceo (GET
/api/agents/me is agent-key auth) — reused verbatim from paperclip-onboarder.py.

What it does (single pass — the charter is deploy-triggered, not drift-triggered, so there is
no reconcile loop; --once is accepted for parity):
  1. Parse companies/<slug>/.paperclip.yaml; select roles with status=="active"; read each
     active role's agents_md + the package COMPANY.md off disk (fail-closed if missing/empty).
     COMPANY.md is mandatory — the live import rejects a payload without it (proven in #58).
  2. Resolve companyId from the CEO key; resolve role→agentId from the company agent list.
  3. Per active role, GET the managed bundle and compare (idempotency) — write only on drift.
  4. On drift, POST /api/companies/import with {source:{type:inline,files:{COMPANY.md, each
     active agents/<role>/AGENTS.md}}, target:{mode:existing_company, companyId}}. If the import
     route is unavailable on this build (404/405/501), fall back to per-role PUT
     /api/agents/{id}/instructions-bundle/file.

Exit codes (mirror the onboarder):
  0  — synced / already in sync / disabled (no-op) / dry-run
  75 — EX_TEMPFAIL: board unreachable, 5xx, or companyId not resolvable yet (retryable next deploy)
  1  — hard error (missing CEO key, bad/missing manifest, active-role file missing, board
       401/403 [expired key — see #42], unexpected status)

Config (env):
  PAPERCLIP_COMPANY_TEMPLATE  company slug to sync (unset → default agentsys-coala)
  PAPERCLIP_BOARD_KEY         board key (pcp_board_*); else ~/.paperclip/auth.json is scanned
  PAPERCLIP_CEO_KEY           CEO bearer key (fallback if ~/.pclip.key absent) — resolves companyId
  PAPERCLIP_API_URL           Paperclip base URL (default http://paperclip.railway.internal:3100)
  PAPERCLIP_COMPANIES_BASE    company packages root (default /app/companies)
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

DEFAULT_API_URL = "http://paperclip.railway.internal:3100"
DEFAULT_COMPANIES_BASE = "/app/companies"
# Fallback company slug when PAPERCLIP_COMPANY_TEMPLATE is unset. agentsys-coala is the only
# packaged company today; replace with the #59 `default-coala` baseline when it lands.
DEFAULT_COMPANY_TEMPLATE = "agentsys-coala"
PCLIP_KEY_FILE = Path.home() / ".pclip.key"
BOARD_AUTH_FILE = Path.home() / ".paperclip" / "auth.json"

MANIFEST_NAME = ".paperclip.yaml"
EXPECTED_SCHEMA = "paperclip/v1"
VALID_STATUSES = ("active", "defined-only")
BUNDLE_FILENAME = "AGENTS.md"  # the ?path= value the managed bundle is keyed by
COMPANY_FILE = "COMPANY.md"    # the live /api/companies/import requires this (proven in #58)

# Exit codes (see module docstring).
EX_OK = 0
EX_HARD = 1
EX_TEMPFAIL = 75


def log(msg: str) -> None:
    """Stderr logging consistent with the bash scripts ([company-sync] prefix)."""
    print(f"[company-sync] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def _ceo_key_or_none() -> str | None:
    """CEO bearer key from ~/.pclip.key, else $PAPERCLIP_CEO_KEY; None if neither."""
    if PCLIP_KEY_FILE.is_file():
        key = PCLIP_KEY_FILE.read_text().strip()
        if key:
            return key
        log(f"WARN: {PCLIP_KEY_FILE} is empty; falling back to PAPERCLIP_CEO_KEY")
    key = os.environ.get("PAPERCLIP_CEO_KEY", "").strip()
    return key or None


def load_ceo_key() -> str:
    """CEO bearer key, or EX_HARD if absent (mirrors paperclip-onboarder.py:153-164)."""
    key = _ceo_key_or_none()
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


def load_board_key() -> str | None:
    """Board key (pcp_board_*) from $PAPERCLIP_BOARD_KEY, else ~/.paperclip/auth.json.
    Returns None when absent — absence is a gate (feature off), not a hard error."""
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
# Company package loading
# ---------------------------------------------------------------------------
def load_manifest(company_dir: Path) -> dict[str, Any]:
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


def select_active_roles(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Roles with status=='active'. defined-only roles are ignored (their agents_md may
    legitimately not exist on disk yet — they activate later via #52–#55)."""
    return [r for r in manifest.get("roles", []) if r.get("status") == "active"]


def read_role_bundle(company_dir: Path, role: dict[str, Any]) -> str:
    """Read an active role's agents_md. Path-traversal guarded; fail-closed (EX_HARD) if the
    file is missing or empty — an active role with no operating frame is a packaging bug."""
    rel = role["agents_md"]
    base = company_dir.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        log(f"ERROR: role {role['name']!r} agents_md {rel!r} escapes {base}")
        sys.exit(EX_HARD)
    if not target.is_file():
        log(f"ERROR: active role {role['name']!r} bundle missing: {target}")
        sys.exit(EX_HARD)
    content = target.read_text()
    if not content.strip():
        log(f"ERROR: active role {role['name']!r} bundle is empty: {target}")
        sys.exit(EX_HARD)
    return content


def read_company_doc(company_dir: Path) -> str:
    """Read the package's COMPANY.md. Required: the live /api/companies/import rejects a
    payload without it ([422] "Company package is missing COMPANY.md", surfaced in #58)."""
    p = company_dir / COMPANY_FILE
    if not p.is_file():
        log(f"ERROR: {p} missing — /api/companies/import requires {COMPANY_FILE}")
        sys.exit(EX_HARD)
    content = p.read_text()
    if not content.strip():
        log(f"ERROR: {p} is empty")
        sys.exit(EX_HARD)
    return content


def collect_definition_files(company_dir: Path, active: list[dict[str, Any]]) -> dict[str, str]:
    """The definition-plane files the import carries: COMPANY.md + each active role's
    AGENTS.md (keyed by repo-relative path). COMPANY.md is mandatory (see read_company_doc)."""
    files = {COMPANY_FILE: read_company_doc(company_dir)}
    for role in active:
        files[role["agents_md"]] = read_role_bundle(company_dir, role)
    return files


def build_import_payload(company_id: str, files: dict[str, str]) -> dict[str, Any]:
    """Pure: the companies/import request body (shape from #56, confirmed in spike #42).
    Keys of `files` are repo-relative paths (e.g. agents/ceo/AGENTS.md). Called by the
    real run, the dry-run, and the tests."""
    return {
        "source": {"type": "inline", "files": dict(files)},
        "target": {"mode": "existing_company", "companyId": company_id},
    }


def _payload_summary(payload: dict[str, Any]) -> str:
    files = payload.get("source", {}).get("files", {})
    target = payload.get("target", {})
    lines = [f"  target: mode={target.get('mode')} companyId={target.get('companyId')}"]
    for path, content in files.items():
        first = content.splitlines()[0] if content else ""
        lines.append(f"  file: {path} ({len(content)} bytes) — {first[:70]}")
    return "import payload:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP — client seam + board operations
# ---------------------------------------------------------------------------
def make_client(api_url: str, bearer: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The single client-construction seam. Production passes no transport (real network);
    tests pass an httpx.MockTransport. Timeout matches paperclip-onboarder.py:408."""
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
    kwargs: dict[str, Any] = {"base_url": api_url, "headers": headers, "timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def resolve_ceo(client: httpx.Client) -> tuple[dict[str, Any] | None, str]:
    """Resolve the CEO agent (id + companyId) the bearer key belongs to, with no hardcoded
    id. Copied verbatim from paperclip-onboarder.py:245-290 (a shared module is a larger
    refactor out of #56's scope). The client here carries the CEO *agent* key."""
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
        return None, ("resolve-ceo: no agent with role=ceo found via /me, chainOfCommand, "
                      "or the company agent list")
    return ({"id": ceo_id, "companyId": company_id},
            f"resolve-ceo: CEO resolved to {ceo_id} (company {company_id})")


def resolve_agent_ids(client: httpx.Client, company_id: str) -> dict[str, str]:
    """Map role→agentId from the company agent list (for per-role readback + PUT fallback).
    Returns {} on any failure; the caller treats an unresolvable id per-role."""
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
    """401/403 → bad/expired board key. Not retryable; never fall back to PUT (same key)."""
    return resp.status_code in (401, 403)


def import_unavailable(resp: httpx.Response) -> bool:
    """True when the import route isn't available on this Paperclip build → use the PUT
    fallback. 404/405/501, or a 400/422 body that names import as unknown/unsupported."""
    if resp.status_code in (404, 405, 501):
        return True
    if resp.status_code in (400, 422):
        body = resp.text.lower()
        return "import" in body and ("not" in body or "unknown" in body or "unsupported" in body)
    return False


def import_company(client: httpx.Client, payload: dict[str, Any]) -> httpx.Response:
    return client.post("/api/companies/import", json=payload)


def put_role_bundle(client: httpx.Client, agent_id: str, content: str) -> httpx.Response:
    return client.put(
        f"/api/agents/{agent_id}/instructions-bundle/file",
        params={"path": BUNDLE_FILENAME},
        headers={"Content-Type": "text/markdown; charset=utf-8"},
        content=content.encode("utf-8"),
    )


def readback_role_bundle(client: httpx.Client, agent_id: str) -> httpx.Response | None:
    """GET the managed bundle file; None on transport error (treated as 'cannot confirm')."""
    try:
        return client.get(
            f"/api/agents/{agent_id}/instructions-bundle/file",
            params={"path": BUNDLE_FILENAME},
        )
    except httpx.HTTPError as exc:
        log(f"readback {agent_id}: GET failed ({exc})")
        return None


def _bundle_content(resp: httpx.Response) -> str:
    """Extract the bundle text from a readback response. The live board returns a JSON
    envelope {"path":…, "content":…} (confirmed in #58); tolerate a raw-text body too."""
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


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _put_fallback(client: httpx.Client, drift: list[dict[str, Any]],
                  files: dict[str, str], agent_ids: dict[str, str]) -> int:
    """Per-role PUT for the drifted roles when the import route is unavailable."""
    statuses: list[str] = []
    for role in drift:
        name = role["name"]
        content = files[role["agents_md"]]
        agent_id = agent_ids.get(name)
        if not agent_id:
            log(f"role {name}: no agent id resolved — cannot PUT fallback")
            statuses.append("error")
            continue
        try:
            resp = put_role_bundle(client, agent_id, content)
        except httpx.HTTPError as exc:
            log(f"role {name}: PUT failed ({exc})")
            statuses.append("temp")
            continue
        if is_auth_failure(resp):
            log(f"role {name}: PUT board auth failed [{resp.status_code}] — board key may be "
                f"expired (see #42)")
            statuses.append("error")
        elif resp.status_code // 100 == 2:
            log(f"role {name}: bundle written via PUT fallback ({len(content)} bytes)")
            statuses.append("ok")
        elif resp.status_code // 100 == 5:
            log(f"role {name}: PUT server error [{resp.status_code}] {resp.text[:200]}")
            statuses.append("temp")
        else:
            log(f"role {name}: PUT unexpected [{resp.status_code}] {resp.text[:200]}")
            statuses.append("error")
    if "error" in statuses:
        return EX_HARD
    if "temp" in statuses:
        return EX_TEMPFAIL
    return EX_OK


def sync_once(api_url: str, companies_root: str, slug: str, dry_run: bool,
              transport: httpx.BaseTransport | None = None) -> int:
    """One sync pass. Gating (template/board key) is done by the caller (main)."""
    company_dir = Path(companies_root) / slug
    if not company_dir.is_dir():
        log(f"ERROR: company package not found: {company_dir} (PAPERCLIP_COMPANY_TEMPLATE={slug})")
        return EX_HARD

    manifest = load_manifest(company_dir)
    active = select_active_roles(manifest)
    if not active:
        log(f"company {slug}: no active roles in manifest — nothing to sync")
        return EX_OK
    files = collect_definition_files(company_dir, active)
    log(f"company {slug}: {len(active)} active role(s) — {', '.join(r['name'] for r in active)} "
        f"(+ {COMPANY_FILE})")

    # Dry-run: build + show the payload without writing. Resolve companyId if possible, else
    # use a placeholder so the payload is shown even with no board reachable.
    if dry_run:
        company_id = "<resolved-from-CEO-key-at-runtime>"
        ceo_key = _ceo_key_or_none()
        if ceo_key:
            try:
                with make_client(api_url, ceo_key, transport) as client:
                    resolved, msg = resolve_ceo(client)
                    log(msg)
                    if resolved and resolved.get("companyId"):
                        company_id = resolved["companyId"]
            except httpx.HTTPError as exc:
                log(f"dry-run: companyId not resolved ({exc}); using placeholder")
        else:
            log("dry-run: no CEO key; companyId placeholder used")
        payload = build_import_payload(company_id, files)
        log("DRY-RUN — would POST /api/companies/import\n" + _payload_summary(payload))
        return EX_OK

    # Real run: resolve companyId + role→agentId via the CEO agent key.
    ceo_key = load_ceo_key()
    with make_client(api_url, ceo_key, transport) as client:
        resolved, msg = resolve_ceo(client)
        log(msg)
        if resolved is None:
            return EX_TEMPFAIL  # board reachable-but-no-CEO or unreachable; retry next deploy
        company_id = resolved.get("companyId")
        if not company_id:
            log("resolve-ceo: resolved CEO has no companyId; cannot target import")
            return EX_TEMPFAIL
        agent_ids = resolve_agent_ids(client, company_id)
    if resolved.get("id") and "ceo" not in agent_ids:
        agent_ids["ceo"] = resolved["id"]

    board_key = load_board_key()
    if board_key is None:  # main gates this, but stay self-contained
        log("no board credential available; skipping")
        return EX_OK

    payload = build_import_payload(company_id, files)
    with make_client(api_url, board_key, transport) as client:
        # Idempotency: only write roles whose managed bundle drifts from disk.
        drift: list[dict[str, Any]] = []
        for role in active:
            name = role["name"]
            agent_id = agent_ids.get(name)
            if agent_id and _in_sync(client, agent_id, files[role["agents_md"]]):
                log(f"role {name}: in sync")
            else:
                drift.append(role)
        if not drift:
            log(f"company {slug}: all active roles in sync — no import needed")
            return EX_OK

        log(f"company {slug}: {len(drift)} role(s) drifted — importing")
        try:
            resp = import_company(client, payload)
        except httpx.HTTPError as exc:
            log(f"import: POST failed ({exc})")
            return EX_TEMPFAIL
        if is_auth_failure(resp):
            log(f"import: board auth failed [{resp.status_code}] — board key may be expired "
                f"(see #42); refresh ~/.paperclip/auth.json or $PAPERCLIP_BOARD_KEY")
            return EX_HARD
        if resp.status_code // 100 == 2:
            log(f"company {slug}: imported {COMPANY_FILE} + {len(drift)} active-role bundle(s)")
            return EX_OK
        if import_unavailable(resp):
            log(f"import route unavailable [{resp.status_code}]; falling back to per-role PUT")
            return _put_fallback(client, drift, files, agent_ids)
        if resp.status_code // 100 == 5:
            log(f"import: server error [{resp.status_code}] {resp.text[:200]}")
            return EX_TEMPFAIL
        log(f"import: unexpected [{resp.status_code}] {resp.text[:200]}")
        return EX_HARD


def resolve_slug() -> tuple[str, bool]:
    """Company slug from PAPERCLIP_COMPANY_TEMPLATE, else DEFAULT_COMPANY_TEMPLATE.
    Returns (slug, is_default)."""
    explicit = os.environ.get("PAPERCLIP_COMPANY_TEMPLATE", "").strip()
    if explicit:
        return explicit, False
    return DEFAULT_COMPANY_TEMPLATE, True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync the selected company package's active-role bundles into Paperclip "
                    "(board-key definition-plane import).")
    parser.add_argument("--once", action="store_true",
                        help="run a single sync pass and exit (the only mode; accepted for "
                             "parity with paperclip-onboarder.py)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and log the import payload without writing (read-only)")
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
    log(f"syncing company {slug} → {api_url}{mode}")
    return sync_once(api_url, companies_root, slug, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
