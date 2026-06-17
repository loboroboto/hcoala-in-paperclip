"""Offline tests for scripts/paperclip-company-sync.py (#56/S8, #82 PUT-per-role contract).

No network: a single httpx.MockTransport fake board backs every path (PUT, GET readback, auth
failure). The fake board encodes the contracts the #58 live bring-up surfaced and #82 acts on:
the instructions-bundle PUT MUST be JSON with BOTH 'path' and 'content' (raw bytes / query-only
path → 400), the readback GET returns a JSON envelope ({"content": ...}), and the sync MUST
NEVER POST /api/companies/import or create an agent (import duplicates agents on an existing
company — the #58 regression). The manifest-dependent tests use **synthetic** packages (a
single CEO-active company) so they're robust to the live manifest's activation state — which
roles are `active` is an operational choice, not a unit-test invariant. Run: `pytest tests/`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "paperclip-company-sync.py"
REAL_COMPANY_DIR = REPO_ROOT / "companies" / "agentsys-coala"


def _load_module():
    spec = importlib.util.spec_from_file_location("paperclip_company_sync", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _make_company(tmp_path: Path, roles: list[tuple[str, str]], slug: str = "testco") -> tuple[str, str]:
    """Write a synthetic company package (manifest + COMPANY.md + each role's AGENTS.md).
    roles = [(name, status), ...]. Returns (companies_root, slug). Decouples the tests from the
    live manifest's activation state."""
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


def _bundle(root: str, slug: str, role: str) -> str:
    return (Path(root) / slug / "agents" / role / "AGENTS.md").read_text()


class FakeBoard:
    """In-memory Paperclip board over httpx.MockTransport. Encodes the #82 live contract:

    - PUT /api/agents/{id}/instructions-bundle/file MUST be JSON with BOTH 'path' and 'content'
      (raw bytes or query-only path → 400 — the live failure of the pre-#82 put_role_bundle).
    - GET readback returns a JSON envelope {"path","size","content"}.
    - /api/companies/import and any agent-creating POST are guardrails: they bump a counter and
      404, so a test can assert the sync NEVER imports or creates an agent (the #58 regression).

    put_status forces a status on PUT (e.g. 401) to exercise auth handling.
    """

    def __init__(self, put_status: int | None = None):
        self.put_status = put_status
        self.store: dict[str, dict[str, str]] = {}   # {agent_id: {path: content}}
        self.agents = [{"id": "ceo-1", "role": "ceo"}]
        self.role_to_id = {a["role"]: a["id"] for a in self.agents}
        self.writes = 0                               # successful PUT writes
        self.import_calls = 0                         # MUST stay 0 (import never called)
        self.created_agents = 0                       # MUST stay 0 (no agent ever created)

    def seed(self, agent_id: str, content: str, path: str = mod.BUNDLE_FILENAME) -> None:
        self.store.setdefault(agent_id, {})[path] = content

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "GET" and path == "/api/agents/me":
            return httpx.Response(200, json={"id": "ceo-1", "role": "ceo", "companyId": "co-1"})
        if method == "GET" and path == "/api/companies/co-1/agents":
            return httpx.Response(200, json=self.agents)
        # Guardrails: the sync must never import a company or create an agent (#58 regression).
        if method == "POST" and path == "/api/companies/import":
            self.import_calls += 1
            return httpx.Response(404, json={"error": "import is not used by this sync"})
        if method == "POST" and path in ("/api/agents", "/api/companies/co-1/agents"):
            self.created_agents += 1
            return httpx.Response(404, json={"error": "agent creation must not happen"})
        if path.endswith("/instructions-bundle/file"):
            agent_id = path.split("/")[3]             # /api/agents/<id>/instructions-bundle/file
            if method == "PUT":
                if self.put_status is not None:
                    return httpx.Response(self.put_status, json={"error": "forced"})
                # Live PUT contract (#82): JSON body with BOTH path and content.
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
                want = request.url.params.get("path", mod.BUNDLE_FILENAME)
                content = self.store.get(agent_id, {}).get(want)
                if content is None:
                    return httpx.Response(404, json={"error": "not found"})
                # Live board returns a JSON envelope, not raw text (confirmed in #58).
                return httpx.Response(200, json={"path": want, "size": len(content),
                                                 "content": content})
        return httpx.Response(404, text=f"unhandled {method} {path}")


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    """Keys via env; point the file-based credential paths at nonexistent files so a real
    ~/.pclip.key or ~/.paperclip/auth.json on the host can't bleed into the tests."""
    monkeypatch.setattr(mod, "PCLIP_KEY_FILE", tmp_path / "absent.pclip.key")
    monkeypatch.setattr(mod, "BOARD_AUTH_FILE", tmp_path / "absent.auth.json")
    monkeypatch.setenv("PAPERCLIP_CEO_KEY", "ceo-key-test")
    monkeypatch.setenv("PAPERCLIP_BOARD_KEY", "pcp_board_test")


def _run(board: FakeBoard, root: str, slug: str, dry_run: bool = False) -> int:
    transport = httpx.MockTransport(board.handler)
    return mod.sync_once("http://board.test", root, slug, dry_run, transport)


# --- Definition files: active roles' AGENTS.md only; COMPANY.md validated, not pushed --------
def test_collect_definition_files_active_only(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active"), ("cto", "defined-only")])
    cdir = Path(root) / slug
    active = mod.select_active_roles(mod.load_manifest(cdir))
    files = mod.collect_definition_files(cdir, active)
    # Active only — the defined-only cto is excluded; COMPANY.md is NOT in the write set (#82).
    assert set(files) == {"agents/ceo/AGENTS.md"}
    assert files["agents/ceo/AGENTS.md"] == _bundle(root, slug, "ceo")


def test_company_md_validated_present_but_not_pushed():
    # COMPANY.md must exist + be non-empty for a valid package (the #81 invariant); validation
    # passes for the real shipped package.
    assert mod.read_company_doc(REAL_COMPANY_DIR).strip()


def test_missing_company_md_is_hard(tmp_path):
    # A package with no COMPANY.md fails closed (EX_HARD via SystemExit).
    (tmp_path / "agents" / "ceo").mkdir(parents=True)
    (tmp_path / "agents" / "ceo" / "AGENTS.md").write_text("x")
    with pytest.raises(SystemExit) as exc:
        mod.read_company_doc(tmp_path)
    assert exc.value.code == mod.EX_HARD


# --- The fixed PUT contract -----------------------------------------------------------------
def test_put_uses_json_body_with_path_and_content():
    # Regression for the pre-#82 bug: put_role_bundle must send JSON {path, content}. The mock
    # 400s anything else, so a 200 + stored content proves the fixed body shape.
    board = FakeBoard()
    transport = httpx.MockTransport(board.handler)
    with mod.make_client("http://board.test", "pcp_board_test", transport) as client:
        resp = mod.put_role_bundle(client, "ceo-1", "hello")
    assert resp.status_code == 200
    assert board.store["ceo-1"][mod.BUNDLE_FILENAME] == "hello"
    assert board.writes == 1


def test_put_rejects_non_json_body():
    # Lock the mock to the live contract: the OLD raw-bytes + query-path shape 400s, which is
    # what makes the test above meaningful.
    board = FakeBoard()
    transport = httpx.MockTransport(board.handler)
    with mod.make_client("http://board.test", "pcp_board_test", transport) as client:
        resp = client.put(
            "/api/agents/ceo-1/instructions-bundle/file",
            params={"path": mod.BUNDLE_FILENAME},
            headers={"Content-Type": "text/markdown; charset=utf-8"},
            content=b"raw markdown",
        )
    assert resp.status_code == 400
    assert board.writes == 0


# --- Sync end-to-end: PUT swaps the bundle; never imports or creates agents ------------------
def test_put_round_trips_ceo_bundle(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FakeBoard()
    assert _run(board, root, slug) == mod.EX_OK
    assert board.store["ceo-1"][mod.BUNDLE_FILENAME] == _bundle(root, slug, "ceo")
    assert board.writes == 1
    assert board.import_calls == 0
    assert board.created_agents == 0

    transport = httpx.MockTransport(board.handler)
    with mod.make_client("http://board.test", "pcp_board_test", transport) as client:
        readback = mod.readback_role_bundle(client, "ceo-1")
    assert readback.status_code == 200
    # readback is a JSON envelope; _bundle_content extracts the verbatim bundle text
    assert mod._bundle_content(readback) == _bundle(root, slug, "ceo")


def test_sync_never_imports_or_creates_agents(tmp_path):
    # The #58 regression guard: a fresh board (CEO bundle absent → drift) must be written via
    # PUT only — no /api/companies/import, no agent creation, no `ceo 2` duplicate.
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FakeBoard()
    assert _run(board, root, slug) == mod.EX_OK
    assert board.import_calls == 0
    assert board.created_agents == 0
    assert len(board.agents) == 1  # still just the CEO


def test_dry_run_logs_puts_without_writing(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FakeBoard()
    assert _run(board, root, slug, dry_run=True) == mod.EX_OK
    assert board.writes == 0
    assert board.store == {}
    assert board.import_calls == 0


def test_idempotent_no_write_when_in_sync(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FakeBoard()
    board.seed("ceo-1", _bundle(root, slug, "ceo"))
    assert _run(board, root, slug) == mod.EX_OK
    assert board.writes == 0   # already in sync → no PUT


def test_put_auth_failure_is_hard(tmp_path):
    root, slug = _make_company(tmp_path, [("ceo", "active")])
    board = FakeBoard(put_status=401)
    assert _run(board, root, slug) == mod.EX_HARD
    assert board.writes == 0        # 401 → hard, never retried
    assert board.import_calls == 0  # and never an import attempt


# --- Slug resolution + gating (main) -----------------------------------------------------
def test_resolve_slug_defaults_to_agentsys_coala(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_COMPANY_TEMPLATE", raising=False)
    assert mod.DEFAULT_COMPANY_TEMPLATE == "agentsys-coala"
    assert mod.resolve_slug() == ("agentsys-coala", True)


def test_resolve_slug_explicit_overrides_default(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_COMPANY_TEMPLATE", "other-co")
    assert mod.resolve_slug() == ("other-co", False)


def test_no_op_when_board_key_absent(monkeypatch):
    # No template set → default agentsys-coala; no board key → no-op before any network.
    monkeypatch.delenv("PAPERCLIP_COMPANY_TEMPLATE", raising=False)
    monkeypatch.delenv("PAPERCLIP_BOARD_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["paperclip-company-sync.py", "--once"])
    assert mod.main() == mod.EX_OK
