"""Offline tests for scripts/paperclip-company-provision.py (#48 — non-CEO provisioning).

No network: a single httpx.MockTransport fake board backs every path. The fake board encodes the
live contract the Phase 0 spike confirmed and the #1994 behaviour the provisioner works around:
import born-wires the adapter (adapterType + adapterConfig) but leaves role="agent" and heartbeat
disabled, so the provisioner reconciles role + heartbeat afterward with the board key. Selection
and the end-to-end flow are driven by **synthetic tmp manifests** (the real agentsys-coala
manifest is intentionally all-`defined-only`, so it must not be the fixture for "provisions").
Idempotency keys by agent **name** (= slug = manifest role name), stable across the role PATCH.
The script filename is hyphenated, so it's loaded by path. Run: `pytest tests/`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "paperclip-company-provision.py"
REAL_COMPANY_DIR = REPO_ROOT / "companies" / "agentsys-coala"


def _load_module():
    spec = importlib.util.spec_from_file_location("paperclip_company_provision", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _make_company(tmp_path: Path, roles: list[tuple[str, str]], slug: str = "testco") -> tuple[str, str]:
    """Write a synthetic company package (manifest + COMPANY.md + each role's AGENTS.md).
    roles = [(name, status), ...]. Returns (companies_root, slug)."""
    cdir = tmp_path / slug
    (cdir / "agents").mkdir(parents=True)
    lines = ["schema: paperclip/v1", f"slug: {slug}", "roles:"]
    for name, status in roles:
        lines += [f"  - name: {name}", f"    status: {status}",
                  f"    agents_md: agents/{name}/AGENTS.md"]
        rdir = cdir / "agents" / name
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "AGENTS.md").write_text(f"# {name} charter\nOperating frame for {name}.\n")
    (cdir / ".paperclip.yaml").write_text("\n".join(lines) + "\n")
    (cdir / "COMPANY.md").write_text("---\nschema: agentcompanies/v1\nslug: testco\nname: Test Co\n---\n# Test Co\n")
    return str(tmp_path), slug


class FakeBoard:
    """In-memory Paperclip board. Models #1994: import born-wires the adapter from
    adapterOverrides but sets role="agent" + heartbeat disabled; the provisioner reconciles.

    import_status → force a status on POST import (e.g. 401). return_no_ids → import 2xx with an
    empty agents array (unparseable-result guard).
    """

    def __init__(self, import_status: int | None = None, return_no_ids: bool = False):
        self.import_status = import_status
        self.return_no_ids = return_no_ids
        self.agents: list[dict] = [{"id": "ceo-1", "name": "hermes", "role": "ceo",
                                    "adapterType": "hermes_remote", "adapterConfig": {},
                                    "runtimeConfig": {}}]
        self.import_calls = 0
        self.patches = 0
        self.created_agents = 0
        self.saw_ceo_in_files = False
        self.last_files: dict[str, str] = {}

    def add_agent(self, agent_id, name, role, adapter_type="hermes_remote",
                  adapter_config=None, runtime_config=None):
        self.agents.append({"id": agent_id, "name": name, "role": role,
                            "adapterType": adapter_type, "adapterConfig": adapter_config or {},
                            "runtimeConfig": runtime_config or {}})

    def _by_id(self, aid):
        return next((a for a in self.agents if a["id"] == aid), None)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "GET" and path == "/api/agents/me":
            return httpx.Response(200, json={"id": "ceo-1", "role": "ceo", "companyId": "co-1"})
        if method == "GET" and path == "/api/companies/co-1/agents":
            return httpx.Response(200, json=self.agents)
        if method == "POST" and path == "/api/companies/import":
            self.import_calls += 1
            if self.import_status is not None:
                return httpx.Response(self.import_status, json={"error": "forced"})
            body = json.loads(request.content)
            files = body["source"]["files"]
            self.last_files = files
            if any(k.startswith("agents/ceo/") for k in files):
                self.saw_ceo_in_files = True            # the invariant this script guards
            if mod.COMPANY_FILE not in files:           # live import contract (#58/#81)
                return httpx.Response(422, json={"error": "Company package is missing COMPANY.md"})
            overrides = body.get("adapterOverrides") or {}
            created = []
            for key in files:
                if key == mod.COMPANY_FILE:
                    continue
                slug = key.split("/")[1]                 # agents/<slug>/AGENTS.md
                ov = overrides.get(slug) or {}
                # #1994: role defaults to "agent" + heartbeat disabled; adapter born-wired.
                self.add_agent(f"{slug}-new", slug, "agent",
                               adapter_type=ov.get("adapterType"),
                               adapter_config=dict(ov.get("adapterConfig") or {}),
                               runtime_config={"heartbeat": {"enabled": False}})
                self.created_agents += 1
                created.append({"slug": slug, "id": f"{slug}-new", "action": "created"})
            agents = [] if self.return_no_ids else created
            return httpx.Response(200, json={"company": {"id": "co-1"}, "agents": agents})
        if method == "GET" and path.startswith("/api/agents/"):
            agent = self._by_id(path.split("/")[3])
            return httpx.Response(200, json=agent) if agent else httpx.Response(404, json={})
        if method == "PATCH" and path.startswith("/api/agents/"):
            agent = self._by_id(path.split("/")[3])
            if agent is None:
                return httpx.Response(404, json={"error": "not found"})
            body = json.loads(request.content)
            if "role" in body:
                agent["role"] = body["role"]
            if "adapterType" in body:
                agent["adapterType"] = body["adapterType"]
            if "adapterConfig" in body:                 # merge mode (no replaceAdapterConfig)
                agent["adapterConfig"] = {**(agent.get("adapterConfig") or {}), **body["adapterConfig"]}
            if "runtimeConfig" in body:
                agent["runtimeConfig"] = body["runtimeConfig"]
            self.patches += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text=f"unhandled {method} {path}")


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PCLIP_KEY_FILE", tmp_path / "absent.pclip.key")
    monkeypatch.setattr(mod, "BOARD_AUTH_FILE", tmp_path / "absent.auth.json")
    monkeypatch.setenv("PAPERCLIP_CEO_KEY", "ceo-key-test")
    monkeypatch.setenv("PAPERCLIP_BOARD_KEY", "pcp_board_test")
    monkeypatch.setenv("RUNNER_AUTH_TOKEN", "runner-token-test")
    for v in ("PAPERCLIP_FLEET_MODEL", "PAPERCLIP_HEARTBEAT_INTERVAL",
              "PAPERCLIP_API_URL", "PAPERCLIP_RUNNER_URL", "PAPERCLIP_COMPANY_TEMPLATE"):
        monkeypatch.delenv(v, raising=False)


def _run(board: FakeBoard, companies_root: str, slug: str, dry_run: bool = False) -> int:
    transport = httpx.MockTransport(board.handler)
    return mod.provision_once("http://board.test", companies_root, slug, dry_run, transport)


def _agent_by_name(board: FakeBoard, name: str) -> dict:
    return next(a for a in board.agents if a["name"] == name)


# --- Selection: active non-CEO only ---------------------------------------------------------
def test_select_provision_roles_active_nonceo_only():
    manifest = {"roles": [
        {"name": "ceo", "status": "active", "agents_md": "agents/ceo/AGENTS.md"},
        {"name": "cto", "status": "active", "agents_md": "agents/cto/AGENTS.md"},
        {"name": "staff-engineer", "status": "defined-only", "agents_md": "agents/staff-engineer/AGENTS.md"},
        {"name": "qa-release-lead", "status": "active", "agents_md": "agents/qa-release-lead/AGENTS.md"},
    ]}
    names = {r["name"] for r in mod.select_provision_roles(manifest)}
    assert names == {"cto", "qa-release-lead"}   # active + non-CEO; ceo + defined-only excluded


def test_real_manifest_has_no_active_nonceo_roles(tmp_path):
    # The shipped agentsys-coala manifest is CEO-active, the rest defined-only → the provisioner
    # is a clean no-op until a role is flipped active.
    board = FakeBoard()
    assert _run(board, str(REAL_COMPANY_DIR.parent), "agentsys-coala") == mod.EX_OK
    assert board.import_calls == 0 and board.patches == 0


def test_paperclip_role_mapping():
    assert mod.paperclip_role("cto") == "cto"
    assert mod.paperclip_role("staff-engineer") == "engineer"
    assert mod.paperclip_role("qa-release-lead") == "qa"
    assert mod.paperclip_role("research-perf-analyst") == "researcher"
    assert mod.paperclip_role("unknown-role") == "unknown-role"


def test_build_import_payload_shape():
    files = {"COMPANY.md": "c", "agents/cto/AGENTS.md": "a"}
    roles = [{"name": "cto", "agents_md": "agents/cto/AGENTS.md"}]
    ac = {"remoteRunnerUrl": "http://runner/run"}
    payload = mod.build_import_payload("co-1", files, roles, ac)
    assert payload["source"] == {"type": "inline", "files": files}
    assert payload["target"] == {"mode": "existing_company", "companyId": "co-1"}
    ov = payload["adapterOverrides"]["cto"]
    assert ov["adapterType"] == "hermes_remote" and ov["role"] == "cto" and ov["adapterConfig"] == ac


def test_collect_provision_files_excludes_ceo(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    company_dir = Path(root) / slug
    roles = mod.select_provision_roles(mod.load_manifest(company_dir))   # [cto]
    files = mod.collect_provision_files(company_dir, roles)
    assert mod.COMPANY_FILE in files
    assert "agents/ceo/AGENTS.md" not in files
    assert "agents/cto/AGENTS.md" in files


# --- End-to-end: create + reconcile (role + heartbeat), CEO untouched ------------------------
def test_provision_creates_and_reconciles(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active"), ("staff-engineer", "active")])
    board = FakeBoard()
    assert _run(board, root, slug) == mod.EX_OK
    assert board.import_calls == 1
    assert board.created_agents == 2
    assert board.saw_ceo_in_files is False
    cto, eng = _agent_by_name(board, "cto"), _agent_by_name(board, "staff-engineer")
    assert cto["role"] == "cto" and eng["role"] == "engineer"          # role PATCHed off "agent"
    for a in (cto, eng):
        assert a["adapterType"] == "hermes_remote"
        assert a["runtimeConfig"]["heartbeat"] == {"enabled": True, "intervalSec": 300}
    assert sum(1 for a in board.agents if a["role"] == "ceo") == 1     # no duplicate CEO


def test_ceo_never_in_import_files(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = FakeBoard()
    _run(board, root, slug)
    assert board.saw_ceo_in_files is False
    assert "agents/ceo/AGENTS.md" not in board.last_files


def test_idempotent_when_in_sync(tmp_path):
    # An already-correct agent: no import, no PATCH.
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("staff-engineer", "active")])
    board = FakeBoard()
    board.add_agent("se-1", "staff-engineer", "engineer",
                    adapter_config=mod.build_adapter_config(),
                    runtime_config={"heartbeat": {"enabled": True, "intervalSec": 300}})
    assert _run(board, root, slug) == mod.EX_OK
    assert board.import_calls == 0    # found by name → not re-imported (role engineer != name)
    assert board.patches == 0         # already in sync → no reconcile PATCH


def test_self_heals_partial_prior_run(tmp_path):
    # A prior run created the agent but left role="agent" / heartbeat off → reconcile fixes it,
    # and it is NOT re-imported (matched by name).
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = FakeBoard()
    board.add_agent("cto-1", "cto", "agent", adapter_config=mod.build_adapter_config(),
                    runtime_config={"heartbeat": {"enabled": False}})
    assert _run(board, root, slug) == mod.EX_OK
    assert board.import_calls == 0
    assert board.patches == 1
    cto = _agent_by_name(board, "cto")
    assert cto["role"] == "cto"
    assert cto["runtimeConfig"]["heartbeat"]["enabled"] is True
    assert sum(1 for a in board.agents if a["name"] == "cto") == 1   # no duplicate


def test_dry_run_no_writes(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = FakeBoard()
    assert _run(board, root, slug, dry_run=True) == mod.EX_OK
    assert board.import_calls == 0 and board.patches == 0 and board.created_agents == 0
    assert len(board.agents) == 1   # only the CEO; nothing created


def test_auth_failure_is_hard(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = FakeBoard(import_status=401)
    assert _run(board, root, slug) == mod.EX_HARD
    assert board.patches == 0        # never reconcile when the import auth-fails


def test_unexpected_response_without_ids_is_hard(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = FakeBoard(return_no_ids=True)
    assert _run(board, root, slug) == mod.EX_HARD


def test_no_op_when_board_key_absent(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_BOARD_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["paperclip-company-provision.py", "--once"])
    assert mod.main() == mod.EX_OK
