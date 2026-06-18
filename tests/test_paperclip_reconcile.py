"""Offline tests for the unified reconciler (scripts/paperclip-reconcile.py + paperclip_common.py).

Consolidates the former per-script suites (onboarder / company-sync / company-provision) and
adds end-to-end coverage the old code couldn't reach: phase_onboard and reconcile_once now run
against an httpx.MockTransport fake board via the client seam in paperclip_common.make_client.

No network. The fake boards encode the live contracts the bring-up surfaced:
  - the instructions-bundle PUT MUST be JSON {path, content} (#82); raw/query-only → 400,
  - readback returns a JSON envelope {"content": …},
  - /api/companies/import duplicates an existing agent, so the sync MUST NEVER import (#58),
  - import born-wires the adapter but leaves role="agent" + heartbeat off (#1994).

Manifest-dependent tests use synthetic packages so they're robust to the live manifest's
activation state. The entrypoint is hyphenated, so it's loaded by path; it puts scripts/ on
sys.path so `import paperclip_common` resolves. Run: `pytest tests/`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "paperclip-reconcile.py"
REAL_COMPANY_DIR = REPO_ROOT / "companies" / "agentsys-coala"


def _load_module():
    spec = importlib.util.spec_from_file_location("paperclip_reconcile", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()
common = mod.common
EX_OK, EX_HARD, EX_TEMPFAIL = mod.EX_OK, mod.EX_HARD, mod.EX_TEMPFAIL


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    """Keys via env; point the file-based credential paths at nonexistent files so a real
    ~/.pclip.key or ~/.paperclip/auth.json on the host can't bleed into the tests."""
    monkeypatch.setattr(common, "PCLIP_KEY_FILE", tmp_path / "absent.pclip.key")
    monkeypatch.setattr(common, "BOARD_AUTH_FILE", tmp_path / "absent.auth.json")
    monkeypatch.setenv("PAPERCLIP_CEO_KEY", "ceo-key-test")
    monkeypatch.setenv("PAPERCLIP_BOARD_KEY", "pcp_board_test")
    monkeypatch.setenv("RUNNER_AUTH_TOKEN", "runner-token-test")
    monkeypatch.setenv("PAPERCLIP_ONBOARD", "1")   # onboard phase on-switch (orchestrator tests)
    for v in ("PAPERCLIP_API_URL", "PAPERCLIP_COMPANY_TEMPLATE"):
        monkeypatch.delenv(v, raising=False)


def _log(_msg: str) -> None:  # silent logger for direct common.* calls
    pass


def _make_company(tmp_path: Path, roles: list[tuple[str, str]], slug: str = "testco") -> tuple[str, str]:
    """Write a synthetic company package (manifest + COMPANY.md + each role's AGENTS.md).
    roles = [(name, status), …]. Returns (companies_root, slug)."""
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
    (cdir / "COMPANY.md").write_text("---\nschema: agentcompanies/v1\nslug: testco\n---\n# Test Co\n")
    return str(tmp_path), slug


def _bundle(root: str, slug: str, role: str) -> str:
    return (Path(root) / slug / "agents" / role / "AGENTS.md").read_text()


# The single source of the fleet adapterConfig in the tests — both _make_registry (what the
# phases load) and _defaults_adapter_config (what in-sync seeding builds to match) derive from
# this, mirroring the production "fleet/agents.yaml defaults is the one source" contract.
REGISTRY_DEFAULTS = {
    "paperclipApiUrl": "http://board.test:3100",
    "remoteRunnerUrl": "http://runner.test:8788/run",
    "runnerAuthTokenEnv": "RUNNER_AUTH_TOKEN",
    "persistSession": True,
    "timeoutSec": 600,
    "model": "test/model",
    "heartbeat": {"enabled": True, "intervalSec": 300},
}


def _make_registry(tmp_path: Path) -> str:
    """A minimal fleet registry (agents.yaml shape) with a CEO resolved from the key, whose
    `defaults` drive the adapterConfig for both phases."""
    p = tmp_path / "agents.yaml"
    reg = {"defaults": REGISTRY_DEFAULTS,
           "companies": [{"ceo": True, "resolveCeoFromKey": True,
                          "agents": [{"name": "hermes", "role": "ceo"}]}]}
    p.write_text(yaml.safe_dump(reg))
    return str(p)


def _defaults_adapter_config(token: str = "runner-token-test") -> dict:
    """The adapterConfig the provision phase now builds from REGISTRY_DEFAULTS — used to seed
    an already-in-sync agent so the idempotency check matches."""
    return mod.build_adapter_target(REGISTRY_DEFAULTS, {}, token)["adapterConfig"]


# ===========================================================================
# common: credentials + manifest + resolve_ceo
# ===========================================================================
def test_load_ceo_key_hard_when_absent(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_CEO_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        common.load_ceo_key(_log)
    assert exc.value.code == EX_HARD


def test_load_runner_token_hard_when_absent(monkeypatch):
    monkeypatch.delenv("RUNNER_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        common.load_runner_token("RUNNER_AUTH_TOKEN", _log)
    assert exc.value.code == EX_HARD


def test_load_board_key_none_when_absent(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_BOARD_KEY", raising=False)
    assert common.load_board_key(_log) is None


def test_find_board_key_scans_nested():
    assert common._find_board_key({"a": {"b": ["x", "pcp_board_xyz"]}}) == "pcp_board_xyz"
    assert common._find_board_key({"a": "nope"}) is None


def test_resolve_slug(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_COMPANY_TEMPLATE", raising=False)
    assert common.DEFAULT_COMPANY_TEMPLATE == "agentsys-coala"
    assert common.resolve_slug() == ("agentsys-coala", True)
    monkeypatch.setenv("PAPERCLIP_COMPANY_TEMPLATE", "other-co")
    assert common.resolve_slug() == ("other-co", False)


def test_read_company_doc_present_and_missing(tmp_path):
    assert common.read_company_doc(REAL_COMPANY_DIR, _log).strip()
    (tmp_path / "agents" / "ceo").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        common.read_company_doc(tmp_path, _log)
    assert exc.value.code == EX_HARD


def _ceo_client(handler) -> httpx.Client:
    return common.make_client("http://board.test", "ceo-key-test", httpx.MockTransport(handler))


def test_resolve_ceo_me_chain_and_list():
    def me_ceo(req):
        return httpx.Response(200, json={"id": "ceo-1", "role": "ceo", "companyId": "co-1"})
    with _ceo_client(me_ceo) as c:
        assert common.resolve_ceo(c)[0] == {"id": "ceo-1", "companyId": "co-1"}

    def via_list(req):
        if req.url.path == "/api/agents/me":
            return httpx.Response(200, json={"id": "w", "role": "engineer", "companyId": "co-1"})
        if req.url.path == "/api/companies/co-1/agents":
            return httpx.Response(200, json=[{"id": "ceo-9", "role": "ceo"}])
        return httpx.Response(404)
    with _ceo_client(via_list) as c:
        assert common.resolve_ceo(c)[0] == {"id": "ceo-9", "companyId": "co-1"}


# ===========================================================================
# PHASE 1 — provision (ported from test_paperclip_company_provision.py)
# ===========================================================================
class ProvisionBoard:
    """Models #1994: import born-wires the adapter from adapterOverrides but sets role="agent"
    + heartbeat disabled; the provisioner reconciles afterward."""

    def __init__(self, import_status=None, return_no_ids=False):
        self.import_status = import_status
        self.return_no_ids = return_no_ids
        self.agents = [{"id": "ceo-1", "name": "hermes", "role": "ceo",
                        "adapterType": "hermes_remote", "adapterConfig": {}, "runtimeConfig": {}}]
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
                self.saw_ceo_in_files = True
            if common.COMPANY_FILE not in files:
                return httpx.Response(422, json={"error": "missing COMPANY.md"})
            overrides = body.get("adapterOverrides") or {}
            created = []
            for key in files:
                if key == common.COMPANY_FILE:
                    continue
                slug = key.split("/")[1]
                ov = overrides.get(slug) or {}
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
            for k in ("role", "adapterType", "runtimeConfig"):
                if k in body:
                    agent[k] = body[k]
            if "adapterConfig" in body:
                agent["adapterConfig"] = {**(agent.get("adapterConfig") or {}), **body["adapterConfig"]}
            self.patches += 1
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text=f"unhandled {method} {path}")


def _run_provision(board, root, slug, dry_run=False, board_key="pcp_board_test"):
    # phase_provision now takes a registry_path (the adapterConfig source) and returns
    # (rc, mutated); helper writes a registry under root and yields just the rc.
    registry = _make_registry(Path(root))
    return mod.phase_provision("http://board.test", root, slug, registry, dry_run, board_key,
                               httpx.MockTransport(board.handler))[0]


def _agent_by_name(board, name):
    return next(a for a in board.agents if a["name"] == name)


def test_select_provision_roles_active_nonceo_only():
    manifest = {"roles": [
        {"name": "ceo", "status": "active", "agents_md": "agents/ceo/AGENTS.md"},
        {"name": "cto", "status": "active", "agents_md": "agents/cto/AGENTS.md"},
        {"name": "staff-engineer", "status": "defined-only", "agents_md": "agents/staff-engineer/AGENTS.md"},
        {"name": "qa-release-lead", "status": "active", "agents_md": "agents/qa-release-lead/AGENTS.md"},
    ]}
    assert {r["name"] for r in mod.select_provision_roles(manifest)} == {"cto", "qa-release-lead"}


def test_real_manifest_provision_selection_is_active_nonceo():
    # The shipped manifest is CEO-only-active today; selection must never include the CEO and
    # only ever active roles (vacuously true when none are active non-CEO).
    roles = mod.select_provision_roles(common.load_manifest(REAL_COMPANY_DIR, _log))
    assert all(r["name"] != "ceo" and r.get("status") == "active" for r in roles)


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
    roles = mod.select_provision_roles(common.load_manifest(company_dir, _log))   # [cto]
    files = mod.collect_provision_files(company_dir, roles)
    assert common.COMPANY_FILE in files
    assert "agents/ceo/AGENTS.md" not in files and "agents/cto/AGENTS.md" in files


def test_provision_creates_and_reconciles(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active"), ("staff-engineer", "active")])
    board = ProvisionBoard()
    assert _run_provision(board, root, slug) == EX_OK
    assert board.import_calls == 1 and board.created_agents == 2 and board.saw_ceo_in_files is False
    cto, eng = _agent_by_name(board, "cto"), _agent_by_name(board, "staff-engineer")
    assert cto["role"] == "cto" and eng["role"] == "engineer"
    for a in (cto, eng):
        assert a["adapterType"] == "hermes_remote"
        assert a["runtimeConfig"]["heartbeat"] == {"enabled": True, "intervalSec": 300}
    assert sum(1 for a in board.agents if a["role"] == "ceo") == 1


def test_provision_ceo_never_in_import_files(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard()
    _run_provision(board, root, slug)
    assert board.saw_ceo_in_files is False and "agents/ceo/AGENTS.md" not in board.last_files


def test_provision_idempotent_when_in_sync(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("staff-engineer", "active")])
    board = ProvisionBoard()
    board.add_agent("se-1", "staff-engineer", "engineer",
                    adapter_config=_defaults_adapter_config(),
                    runtime_config={"heartbeat": {"enabled": True, "intervalSec": 300}})
    assert _run_provision(board, root, slug) == EX_OK
    assert board.import_calls == 0 and board.patches == 0


def test_provision_self_heals_partial_prior_run(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard()
    board.add_agent("cto-1", "cto", "agent", adapter_config=_defaults_adapter_config(),
                    runtime_config={"heartbeat": {"enabled": False}})
    assert _run_provision(board, root, slug) == EX_OK
    assert board.import_calls == 0 and board.patches == 1
    cto = _agent_by_name(board, "cto")
    assert cto["role"] == "cto" and cto["runtimeConfig"]["heartbeat"]["enabled"] is True


def test_provision_dry_run_no_writes(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard()
    assert _run_provision(board, root, slug, dry_run=True) == EX_OK
    assert board.import_calls == 0 and board.patches == 0 and board.created_agents == 0


def test_provision_auth_failure_is_hard(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard(import_status=401)
    assert _run_provision(board, root, slug) == EX_HARD
    assert board.patches == 0


def test_provision_unexpected_response_without_ids_is_hard(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard(return_no_ids=True)
    assert _run_provision(board, root, slug) == EX_HARD


def test_provision_no_op_when_board_key_absent(tmp_path):
    # Real run, no board key → clean no-op before any package read or network.
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard()
    assert _run_provision(board, root, slug, board_key=None) == EX_OK
    assert board.import_calls == 0 and board.created_agents == 0


def test_provision_noop_when_no_active_nonceo(tmp_path):
    # The current shipped state: CEO active, others defined-only → nothing to provision.
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "defined-only")])
    board = ProvisionBoard()
    assert _run_provision(board, root, slug) == EX_OK
    assert board.import_calls == 0


def test_provision_missing_token_is_hard_real_run(tmp_path, monkeypatch):
    # The unified hard error: an active non-CEO role + board key but no runner token → EX_HARD
    # (was a warn-and-proceed in the old provisioner).
    monkeypatch.delenv("RUNNER_AUTH_TOKEN", raising=False)
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard()
    with pytest.raises(SystemExit) as exc:
        _run_provision(board, root, slug)
    assert exc.value.code == EX_HARD


# ===========================================================================
# PHASE 2 — sync (ported from test_paperclip_company_sync.py)
# ===========================================================================
class SyncBoard:
    """Encodes the #82 PUT contract + the #58 never-import guardrail."""

    def __init__(self, put_status=None):
        self.put_status = put_status
        self.store: dict[str, dict[str, str]] = {}
        self.agents = [{"id": "ceo-1", "role": "ceo"}]
        self.writes = 0
        self.import_calls = 0
        self.created_agents = 0

    def seed(self, agent_id, content, path=None):
        self.store.setdefault(agent_id, {})[path or common.BUNDLE_FILENAME] = content

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "GET" and path == "/api/agents/me":
            return httpx.Response(200, json={"id": "ceo-1", "role": "ceo", "companyId": "co-1"})
        if method == "GET" and path == "/api/companies/co-1/agents":
            return httpx.Response(200, json=self.agents)
        if method == "POST" and path == "/api/companies/import":
            self.import_calls += 1
            return httpx.Response(404, json={"error": "import is not used by this sync"})
        if method == "POST" and path in ("/api/agents", "/api/companies/co-1/agents"):
            self.created_agents += 1
            return httpx.Response(404, json={"error": "agent creation must not happen"})
        if path.endswith("/instructions-bundle/file"):
            agent_id = path.split("/")[3]
            if method == "PUT":
                if self.put_status is not None:
                    return httpx.Response(self.put_status, json={"error": "forced"})
                try:
                    body = json.loads(request.content)
                except (json.JSONDecodeError, ValueError):
                    return httpx.Response(400, json={"error": "expected object"})
                if not isinstance(body, dict) or "path" not in body or "content" not in body:
                    return httpx.Response(400, json={"error": "path and content required"})
                self.store.setdefault(agent_id, {})[body["path"]] = body["content"]
                self.writes += 1
                return httpx.Response(200, json={"ok": True})
            if method == "GET":
                want = request.url.params.get("path", common.BUNDLE_FILENAME)
                content = self.store.get(agent_id, {}).get(want)
                if content is None:
                    return httpx.Response(404, json={"error": "not found"})
                return httpx.Response(200, json={"path": want, "size": len(content), "content": content})
        return httpx.Response(404, text=f"unhandled {method} {path}")


def _run_sync(board, root, slug, dry_run=False, board_key="pcp_board_test"):
    # phase_sync now returns (rc, mutated); helper yields just the rc for the rc assertions.
    return mod.phase_sync("http://board.test", root, slug, dry_run, board_key,
                          httpx.MockTransport(board.handler))[0]


def test_collect_definition_files_active_only(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "defined-only")])
    cdir = Path(root) / slug
    active = mod.select_active_roles(common.load_manifest(cdir, _log))
    files = mod.collect_definition_files(cdir, active)
    assert set(files) == {"agents/ceo/AGENTS.md"}
    assert files["agents/ceo/AGENTS.md"] == _bundle(root, slug, "ceo")


def test_put_uses_json_body_with_path_and_content():
    board = SyncBoard()
    with common.make_client("http://board.test", "pcp_board_test", httpx.MockTransport(board.handler)) as client:
        resp = mod.put_role_bundle(client, "ceo-1", "hello")
    assert resp.status_code == 200
    assert board.store["ceo-1"][common.BUNDLE_FILENAME] == "hello" and board.writes == 1


def test_put_rejects_non_json_body():
    board = SyncBoard()
    with common.make_client("http://board.test", "pcp_board_test", httpx.MockTransport(board.handler)) as client:
        resp = client.put("/api/agents/ceo-1/instructions-bundle/file",
                          params={"path": common.BUNDLE_FILENAME},
                          headers={"Content-Type": "text/markdown"}, content=b"raw markdown")
    assert resp.status_code == 400 and board.writes == 0


def test_sync_put_round_trips_and_never_imports(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = SyncBoard()
    assert _run_sync(board, root, slug) == EX_OK
    assert board.store["ceo-1"][common.BUNDLE_FILENAME] == _bundle(root, slug, "ceo")
    assert board.writes == 1 and board.import_calls == 0 and board.created_agents == 0
    assert len(board.agents) == 1


def test_sync_idempotent_when_in_sync(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = SyncBoard()
    board.seed("ceo-1", _bundle(root, slug, "ceo"))
    assert _run_sync(board, root, slug) == EX_OK
    assert board.writes == 0


def test_sync_dry_run_no_writes(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = SyncBoard()
    assert _run_sync(board, root, slug, dry_run=True) == EX_OK
    assert board.writes == 0 and board.store == {} and board.import_calls == 0


def test_sync_put_auth_failure_is_hard(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = SyncBoard(put_status=401)
    assert _run_sync(board, root, slug) == EX_HARD
    assert board.writes == 0 and board.import_calls == 0


def test_sync_no_op_when_board_key_absent(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = SyncBoard()
    assert _run_sync(board, root, slug, board_key=None) == EX_OK
    assert board.writes == 0


# ===========================================================================
# PHASE 3 — onboard (ported from test_paperclip_onboarder.py + new e2e)
# ===========================================================================
DEFAULTS = {
    "paperclipApiUrl": "http://board.test:3100",
    "remoteRunnerUrl": "http://runner.test:8788/run",
    "persistSession": True, "timeoutSec": 600, "model": "deepseek/deepseek-v4-flash",
    "heartbeat": {"enabled": True, "intervalSec": 300},
}


def test_build_adapter_target_maps_fields_and_token():
    desired = mod.build_adapter_target(DEFAULTS, {"name": "hermes", "role": "ceo"}, "tok-literal")
    cfg = desired["adapterConfig"]
    assert desired["adapterType"] == "hermes_remote"
    assert cfg["runnerAuthToken"] == "tok-literal" and cfg["model"] == "deepseek/deepseek-v4-flash"
    assert desired["heartbeat"] == {"enabled": True, "intervalSec": 300}


def test_build_adapter_target_omits_model_when_unset():
    defaults = {k: v for k, v in DEFAULTS.items() if k != "model"}
    assert "model" not in mod.build_adapter_target(defaults, {"name": "h"}, "t")["adapterConfig"]


def test_needs_update_drift():
    desired = mod.build_adapter_target(DEFAULTS, {"name": "h"}, "tok")
    in_sync = {"adapterType": "hermes_remote", "adapterConfig": dict(desired["adapterConfig"]),
               "runtimeConfig": {"heartbeat": dict(desired["heartbeat"])}}
    assert mod.needs_update(in_sync, desired) is False
    assert mod.needs_update({**in_sync, "adapterType": "claude_local"}, desired) is True
    assert mod.needs_update({**in_sync, "adapterConfig": {**desired["adapterConfig"], "model": "x"}}, desired) is True


@pytest.mark.parametrize("status,body,expected", [
    (422, '{"error":"Unknown adapter type: hermes_remote"}', True),
    (400, '{"error":"Validation error: bad adapter type"}', True),
    (400, '{"error":"some unrelated validation"}', False),
    (200, '{"ok":true}', False),
    (409, '{"error":"adapter type conflict"}', False),
])
def test_is_adapter_missing(status, body, expected):
    resp = httpx.Response(status, text=body, request=httpx.Request("PATCH", "http://b/x"))
    assert mod.is_adapter_missing(resp) is expected


class PatchBoard:
    """Records the GET fixture + last PATCH payload, so onboard tests can assert the outcome
    AND whether replaceAdapterConfig was sent (merge vs replace mode)."""

    def __init__(self, current, patch_status=200, patch_body="{}"):
        self.current = current
        self.patch_status = patch_status
        self.patch_body = patch_body
        self.last_payload = None
        self.patched = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(404, json={}) if self.current is None else httpx.Response(200, json=self.current)
        if req.method == "PATCH":
            self.last_payload = json.loads(req.content)
            self.patched = True
            return httpx.Response(self.patch_status, text=self.patch_body)
        return httpx.Response(404)


def _desired():
    return mod.build_adapter_target(DEFAULTS, {"name": "hermes", "role": "ceo"}, "tok")


def _onboard_client(board):
    return common.make_client("http://board.test", "ceo-key-test", httpx.MockTransport(board))


def test_onboard_agent_replace_mode_on_200():
    board = PatchBoard({"adapterType": "claude_local", "adapterConfig": {}, "runtimeConfig": {}})
    with _onboard_client(board) as c:
        status, _ = mod._onboard_agent(c, "ceo-1", _desired(), dry_run=False)
    assert status == "onboarded" and board.last_payload["replaceAdapterConfig"] is True


def test_onboard_agent_merge_mode_preserves_protected_keys():
    board = PatchBoard({"adapterType": "claude_local",
                        "adapterConfig": {"instructionsFilePath": "AGENTS.md"}, "runtimeConfig": {}})
    with _onboard_client(board) as c:
        status, msg = mod._onboard_agent(c, "ceo-1", _desired(), dry_run=False)
    assert status == "onboarded" and "replaceAdapterConfig" not in board.last_payload
    assert "merge mode" in msg


def test_onboard_agent_absent_waiting_synced_error():
    assert mod._onboard_agent.__name__  # sanity
    # absent
    b = PatchBoard(None)
    with _onboard_client(b) as c:
        assert mod._onboard_agent(c, "x", _desired(), False)[0] == "absent"
    # waiting (adapter not installed)
    b = PatchBoard({"adapterType": "claude_local", "adapterConfig": {}, "runtimeConfig": {}},
                   patch_status=422, patch_body='{"error":"Unknown adapter type: hermes_remote"}')
    with _onboard_client(b) as c:
        assert mod._onboard_agent(c, "ceo-1", _desired(), False)[0] == "waiting"
    # synced
    d = _desired()
    b = PatchBoard({"adapterType": "hermes_remote", "adapterConfig": dict(d["adapterConfig"]),
                    "runtimeConfig": {"heartbeat": dict(d["heartbeat"])}})
    with _onboard_client(b) as c:
        assert mod._onboard_agent(c, "ceo-1", d, False)[0] == "synced"
    assert b.patched is False
    # error
    b = PatchBoard({"adapterType": "claude_local", "adapterConfig": {}, "runtimeConfig": {}},
                   patch_status=500, patch_body="boom")
    with _onboard_client(b) as c:
        assert mod._onboard_agent(c, "ceo-1", _desired(), False)[0] == "error"


def test_onboard_agent_dry_run_does_not_patch():
    board = PatchBoard({"adapterType": "claude_local", "adapterConfig": {}, "runtimeConfig": {}})
    with _onboard_client(board) as c:
        status, msg = mod._onboard_agent(c, "ceo-1", _desired(), dry_run=True)
    assert status == "synced" and board.patched is False and "DRY-RUN" in msg


@pytest.mark.parametrize("statuses,expected", [
    (["onboarded", "synced"], EX_OK),
    (["synced", "waiting"], EX_TEMPFAIL),
    (["synced", "absent"], EX_TEMPFAIL),
    (["waiting", "error"], EX_HARD),
])
def test_pass_exit_code(statuses, expected):
    results = [(str(i), s, "") for i, s in enumerate(statuses)]
    assert mod._pass_exit_code(results) == expected


class OnboardBoard:
    """A board with a CEO at claude_local that accepts the onboard PATCH (and an optional
    adapter-missing gate to model waiting-for-#12)."""

    def __init__(self, adapter_missing=False):
        self.adapter_missing = adapter_missing
        self.ceo = {"id": "ceo-1", "role": "ceo", "companyId": "co-1",
                    "adapterType": "claude_local", "adapterConfig": {}, "runtimeConfig": {}}
        self.patched = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        method, path = req.method, req.url.path
        if method == "GET" and path == "/api/agents/me":
            return httpx.Response(200, json={"id": "ceo-1", "role": "ceo", "companyId": "co-1"})
        if method == "GET" and path == "/api/agents/ceo-1":
            return httpx.Response(200, json=self.ceo)
        if method == "PATCH" and path == "/api/agents/ceo-1":
            if self.adapter_missing:
                return httpx.Response(422, json={"error": "Unknown adapter type: hermes_remote"})
            self.patched = True
            self.ceo["adapterType"] = "hermes_remote"
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text=f"unhandled {method} {path}")


def test_phase_onboard_e2e_onboards_ceo(tmp_path):
    board = OnboardBoard()
    rc, results = mod.phase_onboard(_make_registry(tmp_path), "http://board.test", False,
                                    httpx.MockTransport(board.handler))
    assert rc == EX_OK and board.patched is True
    assert any(st == "onboarded" for _, st, _ in results)


def test_phase_onboard_e2e_waiting_when_adapter_missing(tmp_path):
    board = OnboardBoard(adapter_missing=True)
    rc, results = mod.phase_onboard(_make_registry(tmp_path), "http://board.test", False,
                                    httpx.MockTransport(board.handler))
    assert rc == EX_TEMPFAIL and any(st == "waiting" for _, st, _ in results)


def test_phase_onboard_hard_when_no_ceo_key(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPERCLIP_CEO_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        mod.phase_onboard(_make_registry(tmp_path), "http://board.test", False,
                          httpx.MockTransport(OnboardBoard().handler))
    assert exc.value.code == EX_HARD


# ===========================================================================
# Orchestrator — reconcile_once runs all three phases in one pass
# ===========================================================================
class FleetBoard:
    """A combined board for the end-to-end reconcile: a single CEO agent at claude_local with a
    board-managed instruction bundle. Provision must not import (no active non-CEO roles); sync
    PUTs the CEO bundle; onboard PATCHes the CEO adapter. Models the full happy path."""

    def __init__(self):
        self.ceo = {"id": "ceo-1", "name": "hermes", "role": "ceo", "companyId": "co-1",
                    "adapterType": "claude_local",
                    "adapterConfig": {"instructionsFilePath": "AGENTS.md"}, "runtimeConfig": {}}
        self.bundles: dict[str, dict[str, str]] = {}
        self.import_calls = 0
        self.onboard_patched = False
        self.bundle_writes = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        method, path = req.method, req.url.path
        if method == "GET" and path == "/api/agents/me":
            return httpx.Response(200, json={"id": "ceo-1", "role": "ceo", "companyId": "co-1"})
        if method == "GET" and path == "/api/companies/co-1/agents":
            return httpx.Response(200, json=[self.ceo])
        if method == "POST" and path == "/api/companies/import":
            self.import_calls += 1
            return httpx.Response(404, json={"error": "import must not be called"})
        if path.endswith("/instructions-bundle/file"):
            aid = path.split("/")[3]
            if method == "PUT":
                body = json.loads(req.content)
                self.bundles.setdefault(aid, {})[body["path"]] = body["content"]
                self.bundle_writes += 1
                return httpx.Response(200, json={"ok": True})
            if method == "GET":
                want = req.url.params.get("path", common.BUNDLE_FILENAME)
                c = self.bundles.get(aid, {}).get(want)
                return httpx.Response(404) if c is None else httpx.Response(200, json={"path": want, "content": c})
        if method == "GET" and path == "/api/agents/ceo-1":
            return httpx.Response(200, json=self.ceo)
        if method == "PATCH" and path == "/api/agents/ceo-1":
            self.onboard_patched = True
            body = json.loads(req.content)
            self.ceo["adapterType"] = body.get("adapterType", self.ceo["adapterType"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text=f"unhandled {method} {path}")


def test_reconcile_once_full_chain_ceo_only(tmp_path):
    # The current shipped shape: CEO active, others defined-only. One pass must: provision no-op
    # (no import), sync PUT the CEO bundle, onboard PATCH the CEO adapter — all EX_OK.
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "defined-only")])
    board = FleetBoard()
    rc, sig, summary = mod.reconcile_once("http://board.test", root, slug, _make_registry(tmp_path),
                                          False, httpx.MockTransport(board.handler))
    assert rc == EX_OK
    assert summary == {"provision": EX_OK, "sync": EX_OK, "onboard": EX_OK, "aggregate": EX_OK}
    assert board.import_calls == 0          # provision no-op (no active non-CEO)
    assert board.bundle_writes == 1         # sync wrote the CEO bundle
    assert board.bundles["ceo-1"][common.BUNDLE_FILENAME] == _bundle(root, slug, "ceo")
    assert board.onboard_patched is True    # onboard wired the CEO adapter (merge mode)


def test_reconcile_once_dry_run_writes_nothing(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FleetBoard()
    rc, _, _ = mod.reconcile_once("http://board.test", root, slug, _make_registry(tmp_path),
                                  True, httpx.MockTransport(board.handler))
    assert rc == EX_OK
    assert board.import_calls == 0 and board.bundle_writes == 0 and board.onboard_patched is False


def test_reconcile_once_surfaces_hard_failure(tmp_path):
    # A board 401 on the sync PUT → sync EX_HARD → aggregate EX_HARD, surfaced in the summary.
    root, slug = _make_company(tmp_path, [("ceo", "active")])

    class AuthFailBoard(FleetBoard):
        def handler(self, req):
            if req.method == "PUT" and req.url.path.endswith("/instructions-bundle/file"):
                return httpx.Response(401, json={"error": "expired board key"})
            return super().handler(req)

    board = AuthFailBoard()
    rc, _, summary = mod.reconcile_once("http://board.test", root, slug, _make_registry(tmp_path),
                                        False, httpx.MockTransport(board.handler))
    assert rc == EX_HARD and summary["sync"] == EX_HARD and summary["aggregate"] == EX_HARD


def test_write_status_breadcrumb_contents(tmp_path, monkeypatch):
    status_file = tmp_path / "reconcile.status"
    monkeypatch.setenv("RECONCILE_STATUS_FILE", str(status_file))
    mod.write_status({"provision": EX_OK, "sync": EX_TEMPFAIL, "onboard": EX_OK, "aggregate": EX_TEMPFAIL})
    text = status_file.read_text()
    assert "reconcile tempfail" in text
    assert "sync: tempfail (75)" in text and "provision: ok (0)" in text


def test_run_once_writes_status_breadcrumb(tmp_path, monkeypatch):
    # run_once → reconcile_once → write_status, fully offline via the transport seam.
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    status_file = tmp_path / "reconcile.status"
    monkeypatch.setenv("RECONCILE_STATUS_FILE", str(status_file))
    board = FleetBoard()
    rc = mod.run_once("http://board.test", root, slug, _make_registry(tmp_path), False,
                      httpx.MockTransport(board.handler))
    assert rc == EX_OK
    assert status_file.is_file() and "reconcile ok" in status_file.read_text()


# ===========================================================================
# Review fixes — gating, per-phase isolation, mutation signal, breadcrumb WARN
# ===========================================================================
def test_provision_returns_mutated_flag(tmp_path):
    # mutated=True when a create/PATCH happens (self-heal); False when already in sync.
    reg = _make_registry(tmp_path)
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    t = httpx.MockTransport(ProvisionBoard().handler)
    rc, mutated = mod.phase_provision("http://board.test", root, slug, reg, False, "pcp_board_test", t)
    assert rc == EX_OK and mutated is True            # created cto this pass

    board = ProvisionBoard()
    board.add_agent("cto-1", "cto", "cto", adapter_config=_defaults_adapter_config(),
                    runtime_config={"heartbeat": {"enabled": True, "intervalSec": 300}})
    rc, mutated = mod.phase_provision("http://board.test", root, slug, reg, False, "pcp_board_test",
                                      httpx.MockTransport(board.handler))
    assert rc == EX_OK and mutated is False           # already in sync → no mutation

    # No active non-CEO roles → no work, not mutated.
    root2, slug2 = _make_company(tmp_path, [("ceo", "active")], slug="ceoonly")
    rc, mutated = mod.phase_provision("http://board.test", root2, slug2, reg, False, "pcp_board_test",
                                      httpx.MockTransport(ProvisionBoard().handler))
    assert rc == EX_OK and mutated is False


def test_sync_returns_mutated_flag(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = SyncBoard()
    rc, mutated = mod.phase_sync("http://board.test", root, slug, False, "pcp_board_test",
                                 httpx.MockTransport(board.handler))
    assert rc == EX_OK and mutated is True            # wrote the CEO bundle (drift)

    board.seed("ceo-1", _bundle(root, slug, "ceo"))
    rc, mutated = mod.phase_sync("http://board.test", root, slug, False, "pcp_board_test",
                                 httpx.MockTransport(board.handler))
    assert rc == EX_OK and mutated is False           # in sync → no write


def test_reconcile_once_skips_onboard_when_disabled(tmp_path, monkeypatch):
    # PAPERCLIP_ONBOARD unset → onboard phase is skipped (provision/sync still gated on board key).
    monkeypatch.delenv("PAPERCLIP_ONBOARD", raising=False)
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FleetBoard()
    rc, _, summary = mod.reconcile_once("http://board.test", root, slug, _make_registry(tmp_path),
                                        False, httpx.MockTransport(board.handler))
    assert board.onboard_patched is False             # onboard skipped
    assert board.bundle_writes == 1                    # sync still ran (board key present)
    assert summary["onboard"] == EX_OK and rc == EX_OK


def test_reconcile_once_isolates_a_phase_fault(tmp_path):
    # A fatal company-package fault (an active non-CEO role with a missing AGENTS.md) makes the
    # board-key phases sys.exit — but that MUST NOT propagate or take down onboard, the
    # regression the review caught. provision/sync → EX_HARD (isolated), onboard still runs.
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    (Path(root) / slug / "agents" / "cto" / "AGENTS.md").unlink()   # active role, no charter
    board = FleetBoard()
    rc, _, summary = mod.reconcile_once("http://board.test", root, slug, _make_registry(tmp_path),
                                        False, httpx.MockTransport(board.handler))
    assert summary["provision"] == EX_HARD             # provision's SystemExit isolated to EX_HARD
    assert board.onboard_patched is True               # onboard STILL ran despite the package fault
    assert rc == EX_HARD                               # surfaced in the aggregate


def test_reconcile_once_sig_changes_on_sync_write(tmp_path):
    # A drift-correcting write must be a detectable transition (so the loop flushes its logs
    # immediately rather than burying the self-heal until the hourly heartbeat).
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FleetBoard()
    t = httpx.MockTransport(board.handler)
    _, sig_write, _ = mod.reconcile_once("http://board.test", root, slug, _make_registry(tmp_path), False, t)
    _, sig_insync, _ = mod.reconcile_once("http://board.test", root, slug, _make_registry(tmp_path), False, t)
    assert sig_write != sig_insync                     # write pass vs in-sync pass differ


def test_write_status_warns_directly_on_unwritable_path(tmp_path, monkeypatch, capsys):
    # Finding #7: a breadcrumb-write failure must reach stderr, not be swallowed by the loop's
    # capture buffer. Point the status file under a regular file so mkdir(parents) fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    monkeypatch.setenv("RECONCILE_STATUS_FILE", str(blocker / "nested" / "reconcile.status"))
    mod._capture = []                                  # simulate loop capture mode
    try:
        mod.write_status({"provision": EX_OK, "sync": EX_OK, "onboard": EX_OK, "aggregate": EX_OK})
    finally:
        mod._capture = None
    err = capsys.readouterr().err
    assert "could not write status breadcrumb" in err  # printed directly, not buffered/lost


# ===========================================================================
# Single source — provision (specialist) and onboard (CEO) share fleet defaults
# ===========================================================================
def test_provision_and_onboard_share_one_adapterconfig_source(tmp_path, monkeypatch):
    # The drift this change fixes: provision and onboard both build the adapterConfig from the
    # SAME fleet `defaults`. Even with PAPERCLIP_FLEET_MODEL unset (the OLD provision model
    # source), the specialist gets the fleet model — identical to the CEO. Pre-change, provision
    # read env (→ no model) while onboard read fleet (→ a model), so they could diverge.
    monkeypatch.delenv("PAPERCLIP_FLEET_MODEL", raising=False)
    reg = _make_registry(tmp_path)
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "active")])
    board = ProvisionBoard()
    mod.phase_provision("http://board.test", root, slug, reg, False, "pcp_board_test",
                        httpx.MockTransport(board.handler))
    specialist = _agent_by_name(board, "cto")["adapterConfig"]
    ceo = mod.build_adapter_target(REGISTRY_DEFAULTS, {"role": "ceo"}, "runner-token-test")["adapterConfig"]
    assert specialist["model"] == ceo["model"] == "test/model"            # fleet, not env
    assert specialist["remoteRunnerUrl"] == ceo["remoteRunnerUrl"]
    assert specialist["paperclipApiUrl"] == ceo["paperclipApiUrl"]
