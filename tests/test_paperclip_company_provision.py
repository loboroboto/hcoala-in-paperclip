"""Offline tests for scripts/paperclip-company-provision.py (#48 — non-CEO provisioning).

No network: a single httpx.MockTransport fake board backs every path. The fake board encodes
the contract the Phase 0 spike will confirm against the live build, and in particular the #1994
behaviour the provisioner works around: company import creates an agent with role="agent"
(ignoring the manifest/override role), so the provisioner PATCHes the role afterward. It also
guards the load-bearing invariant — the CEO is NEVER in an import payload (so a duplicate CEO,
the #58 failure, is impossible). The script filename is hyphenated, so it's loaded by path.
Run: `pytest tests/`.
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
COMPANIES_DIR = REPO_ROOT / "companies"
COMPANY_DIR = COMPANIES_DIR / "agentsys-coala"


def _load_module():
    spec = importlib.util.spec_from_file_location("paperclip_company_provision", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()

NON_CEO_ROLES = {"cto", "staff-engineer", "qa-release-lead", "research-perf-analyst"}


class FakeBoard:
    """In-memory Paperclip board over httpx.MockTransport.

    import_status   → force a status on POST import (e.g. 401) to exercise auth handling.
    return_no_ids   → import 2xx but with an empty agents array (unparseable result guard).
    Models #1994: a freshly imported agent gets role="agent" regardless of the override; the
    provisioner's post-import PATCH is what sets the real role.
    """

    def __init__(self, import_status: int | None = None, return_no_ids: bool = False):
        self.import_status = import_status
        self.return_no_ids = return_no_ids
        self.agents: list[dict[str, str]] = [{"id": "ceo-1", "role": "ceo"}]
        self.import_calls = 0
        self.role_patches = 0
        self.created_agents = 0
        self.saw_ceo_in_files = False
        self.last_files: dict[str, str] = {}

    def add_agent(self, agent_id: str, role: str) -> None:
        self.agents.append({"id": agent_id, "role": role})

    def _agent(self, agent_id: str) -> dict[str, str] | None:
        return next((a for a in self.agents if a["id"] == agent_id), None)

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
            files = json.loads(request.content)["source"]["files"]
            self.last_files = files
            if any(k.startswith("agents/ceo/") for k in files):
                self.saw_ceo_in_files = True   # the invariant this whole script guards
            if mod.COMPANY_FILE not in files:  # live import contract (#58/#81)
                return httpx.Response(422, json={"error": "Company package is missing COMPANY.md"})
            created = []
            for key in files:
                if key == mod.COMPANY_FILE:
                    continue
                slug = key.split("/")[1]                 # agents/<slug>/AGENTS.md
                aid = f"{slug}-new"
                # #1994: role defaults to "agent" on import, ignoring the override's role.
                self.add_agent(aid, "agent")
                self.created_agents += 1
                created.append({"slug": slug, "id": aid, "action": "created"})
            agents = [] if self.return_no_ids else created
            return httpx.Response(200, json={"company": {"id": "co-1", "action": "updated"},
                                             "agents": agents})
        if method == "PATCH" and path.startswith("/api/agents/"):
            agent_id = path.split("/")[3]
            agent = self._agent(agent_id)
            if agent is None:
                return httpx.Response(404, json={"error": "not found"})
            body = json.loads(request.content)
            if "role" in body:
                agent["role"] = body["role"]
                self.role_patches += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text=f"unhandled {method} {path}")


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PCLIP_KEY_FILE", tmp_path / "absent.pclip.key")
    monkeypatch.setattr(mod, "BOARD_AUTH_FILE", tmp_path / "absent.auth.json")
    monkeypatch.setenv("PAPERCLIP_CEO_KEY", "ceo-key-test")
    monkeypatch.setenv("PAPERCLIP_BOARD_KEY", "pcp_board_test")
    monkeypatch.setenv("RUNNER_AUTH_TOKEN", "runner-token-test")
    monkeypatch.delenv("PAPERCLIP_FLEET_MODEL", raising=False)


def _run(board: FakeBoard, dry_run: bool = False) -> int:
    transport = httpx.MockTransport(board.handler)
    return mod.provision_once("http://board.test", str(COMPANIES_DIR), "agentsys-coala", dry_run, transport)


# --- Selection + invariant: the CEO is never provisioned ------------------------------------
def test_select_provision_roles_excludes_ceo():
    roles = mod.select_provision_roles(mod.load_manifest(COMPANY_DIR))
    names = {r["name"] for r in roles}
    assert "ceo" not in names
    assert names == NON_CEO_ROLES


def test_collect_provision_files_excludes_ceo():
    roles = mod.select_provision_roles(mod.load_manifest(COMPANY_DIR))
    files = mod.collect_provision_files(COMPANY_DIR, roles)
    assert mod.COMPANY_FILE in files                     # import requires it (#58/#81)
    assert "agents/ceo/AGENTS.md" not in files           # the CEO is never in a payload
    assert {"agents/cto/AGENTS.md", "agents/staff-engineer/AGENTS.md",
            "agents/qa-release-lead/AGENTS.md", "agents/research-perf-analyst/AGENTS.md"} <= set(files)


def test_paperclip_role_mapping():
    assert mod.paperclip_role("cto") == "cto"
    assert mod.paperclip_role("staff-engineer") == "engineer"
    assert mod.paperclip_role("qa-release-lead") == "qa"
    assert mod.paperclip_role("research-perf-analyst") == "researcher"
    assert mod.paperclip_role("unknown-role") == "unknown-role"  # pass-through


def test_build_import_payload_shape():
    files = {"COMPANY.md": "c", "agents/cto/AGENTS.md": "a"}
    roles = [{"name": "cto", "agents_md": "agents/cto/AGENTS.md"}]
    ac = {"remoteRunnerUrl": "http://runner/run"}
    payload = mod.build_import_payload("co-1", files, roles, ac)
    assert payload["source"] == {"type": "inline", "files": files}
    assert payload["target"] == {"mode": "existing_company", "companyId": "co-1"}
    ov = payload["adapterOverrides"]["cto"]
    assert ov["adapterType"] == "hermes_remote"
    assert ov["role"] == "cto"
    assert ov["adapterConfig"] == ac


# --- End-to-end: create non-CEO agents, fix role, never touch the CEO ------------------------
def test_provision_creates_nonceo_agents_and_patches_role():
    board = FakeBoard()
    assert _run(board) == mod.EX_OK
    assert board.import_calls == 1
    assert board.created_agents == 4
    assert board.role_patches == 4
    assert board.saw_ceo_in_files is False
    # Each new agent's role was corrected from the #1994 "agent" default to the mapped enum.
    by_id = {a["id"]: a["role"] for a in board.agents}
    assert by_id["cto-new"] == "cto"
    assert by_id["staff-engineer-new"] == "engineer"
    assert by_id["qa-release-lead-new"] == "qa"
    assert by_id["research-perf-analyst-new"] == "researcher"
    # Exactly one CEO — no duplicate (the #58 failure mode is impossible).
    assert sum(1 for a in board.agents if a["role"] == "ceo") == 1


def test_ceo_never_in_import_files():
    board = FakeBoard()
    _run(board)
    assert board.saw_ceo_in_files is False
    assert "agents/ceo/AGENTS.md" not in board.last_files


def test_idempotent_skips_existing_role():
    board = FakeBoard()
    board.add_agent("cto-existing", "cto")          # cto already provisioned
    assert _run(board) == mod.EX_OK
    assert board.import_calls == 1
    assert "agents/cto/AGENTS.md" not in board.last_files   # not re-imported
    assert board.created_agents == 3
    assert sum(1 for a in board.agents if a["role"] == "cto") == 1  # no duplicate cto


def test_no_import_when_all_provisioned():
    board = FakeBoard()
    # Idempotency is keyed by the manifest role name (resolve_agent_ids → role→id), so seed an
    # existing agent under each non-CEO manifest role name.
    board.agents = [{"id": "ceo-1", "role": "ceo"}] + [
        {"id": f"{r}-x", "role": r} for r in NON_CEO_ROLES
    ]
    assert _run(board) == mod.EX_OK
    assert board.import_calls == 0


def test_dry_run_no_writes():
    board = FakeBoard()
    assert _run(board, dry_run=True) == mod.EX_OK
    assert board.import_calls == 0
    assert board.role_patches == 0
    assert board.created_agents == 0
    assert len(board.agents) == 1   # only the CEO; nothing created


def test_auth_failure_is_hard():
    board = FakeBoard(import_status=401)
    assert _run(board) == mod.EX_HARD
    assert board.role_patches == 0   # never patch when the import auth-fails


def test_unexpected_response_without_ids_is_hard():
    board = FakeBoard(return_no_ids=True)
    assert _run(board) == mod.EX_HARD
    assert board.role_patches == 0


def test_no_op_when_board_key_absent(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_COMPANY_TEMPLATE", raising=False)
    monkeypatch.delenv("PAPERCLIP_BOARD_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["paperclip-company-provision.py", "--once"])
    assert mod.main() == mod.EX_OK
