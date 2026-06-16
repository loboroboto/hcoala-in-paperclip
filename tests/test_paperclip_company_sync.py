"""Offline tests for scripts/paperclip-company-sync.py (#56/S8, #58 live contract).

No network: a single httpx.MockTransport fake board backs every path (import, PUT
fallback, GET readback, auth failure). The fake board encodes the contract the #58 live
bring-up surfaced — /api/companies/import requires COMPANY.md, and the readback GET returns
a JSON envelope ({"content": ...}). The script filename is hyphenated, so it's loaded as a
module by file path. Run: `pytest tests/`.
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
COMPANIES_DIR = REPO_ROOT / "companies"
CEO_BUNDLE = COMPANIES_DIR / "agentsys-coala" / "agents" / "ceo" / "AGENTS.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("paperclip_company_sync", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _expected_ceo_content() -> str:
    return CEO_BUNDLE.read_text()


class FakeBoard:
    """In-memory Paperclip board over httpx.MockTransport.

    mode='import'  → POST /api/companies/import stores each role's AGENTS.md.
    mode='legacy'  → POST import returns 404 so the script falls back to per-role PUT.
    import_status  → force a status on POST import (e.g. 401) to exercise auth handling.
    """

    def __init__(self, mode: str = "import", import_status: int | None = None):
        self.mode = mode
        self.import_status = import_status
        self.store: dict[str, dict[str, str]] = {}   # {agent_id: {path: content}}
        self.company_doc: str | None = None          # COMPANY.md last imported
        self.agents = [{"id": "ceo-1", "role": "ceo"}]
        self.role_to_id = {a["role"]: a["id"] for a in self.agents}
        self.writes = 0                               # successful import/PUT writes

    def seed(self, agent_id: str, content: str, path: str = mod.BUNDLE_FILENAME) -> None:
        self.store.setdefault(agent_id, {})[path] = content

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "GET" and path == "/api/agents/me":
            return httpx.Response(200, json={"id": "ceo-1", "role": "ceo", "companyId": "co-1"})
        if method == "GET" and path == "/api/companies/co-1/agents":
            return httpx.Response(200, json=self.agents)
        if method == "POST" and path == "/api/companies/import":
            if self.import_status is not None:
                return httpx.Response(self.import_status, json={"error": "forced"})
            if self.mode == "legacy":
                return httpx.Response(404, json={"error": "import route not found"})
            files = json.loads(request.content)["source"]["files"]
            if "COMPANY.md" not in files:            # live import contract (surfaced in #58)
                return httpx.Response(422, json={"error": "Company package is missing COMPANY.md"})
            self.company_doc = files["COMPANY.md"]
            for rel, content in files.items():
                if rel == "COMPANY.md":
                    continue
                role = rel.split("/")[1]              # agents/<role>/AGENTS.md
                agent_id = self.role_to_id.get(role)
                if agent_id:
                    self.store.setdefault(agent_id, {})[mod.BUNDLE_FILENAME] = content
                    self.writes += 1
            return httpx.Response(200, json={"imported": len(files)})
        if path.endswith("/instructions-bundle/file"):
            agent_id = path.split("/")[3]             # /api/agents/<id>/instructions-bundle/file
            want = request.url.params.get("path", mod.BUNDLE_FILENAME)
            if method == "PUT":
                self.store.setdefault(agent_id, {})[want] = request.content.decode("utf-8")
                self.writes += 1
                return httpx.Response(200, json={"ok": True})
            if method == "GET":
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


def _run(board: FakeBoard, dry_run: bool = False) -> int:
    transport = httpx.MockTransport(board.handler)
    return mod.sync_once("http://board.test", str(COMPANIES_DIR), "agentsys-coala", dry_run, transport)


# --- Acceptance #1: --dry-run builds a valid payload from companies/agentsys-coala/ -------
def test_collect_definition_files_includes_company_md():
    company_dir = COMPANIES_DIR / "agentsys-coala"
    active = mod.select_active_roles(mod.load_manifest(company_dir))
    files = mod.collect_definition_files(company_dir, active)
    # COMPANY.md is mandatory (live import contract, #58); plus only the active (ceo) bundle —
    # the four defined-only roles are excluded.
    assert set(files) == {"COMPANY.md", "agents/ceo/AGENTS.md"}
    assert files["agents/ceo/AGENTS.md"] == _expected_ceo_content()
    assert files["COMPANY.md"].strip()


def test_build_import_payload_shape():
    files = {"COMPANY.md": "c", "agents/ceo/AGENTS.md": "a"}
    payload = mod.build_import_payload("co-1", files)
    assert payload["source"]["type"] == "inline"
    assert payload["source"]["files"] == files
    assert payload["target"] == {"mode": "existing_company", "companyId": "co-1"}


def test_import_requires_company_md():
    # Regression for #58: the live board rejects an import without COMPANY.md; the mock
    # encodes that contract, so a payload omitting it must 422 (and the sync always sends it).
    board = FakeBoard(mode="import")
    transport = httpx.MockTransport(board.handler)
    payload = mod.build_import_payload("co-1", {"agents/ceo/AGENTS.md": "x"})
    with mod.make_client("http://board.test", "pcp_board_test", transport) as client:
        resp = mod.import_company(client, payload)
    assert resp.status_code == 422
    assert "COMPANY.md" in resp.text


def test_dry_run_builds_payload_without_writing():
    board = FakeBoard(mode="import")
    assert _run(board, dry_run=True) == mod.EX_OK
    assert board.writes == 0
    assert board.store == {}


# --- Acceptance #2: import round-trips the CEO AGENTS.md; readback returns it verbatim ----
def test_import_round_trips_ceo_bundle():
    board = FakeBoard(mode="import")
    assert _run(board) == mod.EX_OK
    assert board.store["ceo-1"][mod.BUNDLE_FILENAME] == _expected_ceo_content()
    assert board.company_doc and "AgentSys CoALA" in board.company_doc  # COMPANY.md imported

    transport = httpx.MockTransport(board.handler)
    with mod.make_client("http://board.test", "pcp_board_test", transport) as client:
        readback = mod.readback_role_bundle(client, "ceo-1")
    assert readback.status_code == 200
    # readback is a JSON envelope; _bundle_content extracts the verbatim bundle text
    assert mod._bundle_content(readback) == _expected_ceo_content()


def test_legacy_board_falls_back_to_put():
    board = FakeBoard(mode="legacy")
    assert _run(board) == mod.EX_OK
    assert board.store["ceo-1"][mod.BUNDLE_FILENAME] == _expected_ceo_content()


def test_idempotent_no_write_when_in_sync():
    board = FakeBoard(mode="import")
    board.seed("ceo-1", _expected_ceo_content())
    assert _run(board) == mod.EX_OK
    assert board.writes == 0   # already in sync → no import/PUT


def test_auth_failure_is_hard_and_no_fallback():
    board = FakeBoard(mode="import", import_status=401)
    assert _run(board) == mod.EX_HARD
    assert board.writes == 0   # 401 must not trigger the PUT fallback


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
