# Interchangeable Paperclip companies

Git-tracked **company packages** that can be hot-swapped via env var. A package
is a self-contained Paperclip-format description of a multi-role org: a
narrative charter, per-role operating frames, and a machine-readable manifest.

**Status (S1, issue #49):** scaffold and conventions only. Nothing here is read
at runtime yet — the sync script (S8, #56) and bootstrap wiring (S9, #57) are
the slices that turn this directory into behaviour. Until then, the contents
just ride the image and document the contract.

---

## Package format

```
companies/
└── <slug>/
    ├── COMPANY.md               # narrative charter (definition plane)
    ├── agents/
    │   └── <role>/
    │       └── AGENTS.md        # per-role operating frame
    └── .paperclip.yaml          # machine-readable manifest
```

`<slug>` is the package id used everywhere downstream — file paths, env vars,
log lines, the Paperclip board-key namespace. Kebab-case, no underscores
(e.g. `agentsys-coala`, `default-coala`).

### `COMPANY.md`

Narrative charter. Mission, decision pipeline ("discovery ≠ planning ≠
implementation ≠ review ≠ shipping"), values, scope guardrails — whatever the
human needs to understand the org. Front matter declares the schema:

```yaml
---
schema: agentcompanies/v1
slug: <slug>
name: <human-readable name>
---
```

### `agents/<role>/AGENTS.md`

Per-role operating frame, written for the role's adapter. Stylistic anchor is
[`hermes-config/AGENTS.md`](../hermes-config/AGENTS.md) — same Markdown
discipline (numbered sections, explicit memory modules, action space, decision
cycle, operational conventions). One file per role; the path is the contract.

### `.paperclip.yaml`

Minimal manifest. The roles list is the source of truth for **active vs
defined-only** (see below):

```yaml
schema: paperclip/v1
slug: <slug>
roles:
  - name: ceo
    status: active
    agents_md: agents/ceo/AGENTS.md
  - name: cto
    status: defined-only
    agents_md: agents/cto/AGENTS.md
  # ...
```

---

## Schema labels

| Label                  | Where                         | Meaning                                    |
| ---------------------- | ----------------------------- | ------------------------------------------ |
| `agentcompanies/v1`    | `COMPANY.md` front matter     | Charter file follows the v1 narrative shape. |
| `paperclip/v1`         | `.paperclip.yaml` top level   | Manifest follows the v1 machine-readable shape. |

These strings are a **contract**. Downstream slices (the sync in S8, the
bootstrap wiring in S9) parse them to decide compatibility. Bump the suffix
only on a breaking change.

---

## Active vs defined-only roles

A role's `status:` in `.paperclip.yaml` decides whether the deployment
materializes it:

- **`active`** — role participates in the adapter plane. The onboarder wires
  it into Hermes peers/channels/transports; messages can reach it; it runs.
- **`defined-only`** — role exists in the charter for legibility and future
  self-expansion (#21), but no adapter is instantiated. The role's
  `AGENTS.md` rides the image; nothing else happens.

**Default is `defined-only`.** A package must explicitly opt a role into
`active`. This keeps a new package safe: copying it in cannot silently spin up
agents.

`agentsys-coala` (S2, #50) ships with **CEO active, the other four
defined-only** until self-expansion (#21) is wired.

---

## Two-plane model

Companies map cleanly onto the durability split already documented in the
root [`README.md` §"Durability Story"](../README.md#durability-story) and the
group-operation frame in
[`hermes-config/AGENTS.md` §6](../hermes-config/AGENTS.md#6-group-operation):

| Plane               | Auth        | What lives here                                                | Mirror in this repo                              |
| ------------------- | ----------- | -------------------------------------------------------------- | ------------------------------------------------ |
| **Definition**      | board-key   | `COMPANY.md` + each role's `AGENTS.md`. Charter / system prompt material. | "Architecture" half of the durability story — git-tracked, fresh from the repo on every deploy. |
| **Adapter**         | agent-key   | Active roles wired to Hermes peers, channels, transports. The actual running surface. | "State" half — volume-backed, mutable, instantiated by the onboarder. |

Board-key writes mutate the charter. Agent-key writes mutate the running
adapters. Tools that only have an agent key cannot rewrite the charter (and
get 403s if they try, per spike #42).

---

## Selector

```
PAPERCLIP_COMPANY_TEMPLATE=<slug>
```

Picks which `companies/<slug>/` package this deployment treats as active.

- Unset → falls back to the default slug (`agentsys-coala` for now, until S11
  (#59) ships `default-coala`). The **board credential**
  (`$PAPERCLIP_BOARD_KEY` or `~/.paperclip/auth.json`) is the effective on/off
  switch — with no board key the sync no-ops.
- Set to a slug with no matching directory → the sync (S8) errors loudly.
- Changing the value and redeploying is how you **switch companies** (see
  runbook below).

Read by the sync as of S8 (#56); launched at boot by the overlay
(`scripts/bootstrap-overlay.d/paperclip.sh`) as of S8/S9 (#56/#57).

---

## Switch runbook

Mirrors the SKILL.md structure used in `hermes-config/skills/*/SKILL.md`. The
bootstrap wiring (S8/S9, #56/#57) is live; the first live bring-up that proves it
end-to-end is S10 (#58).

### Prerequisites

- The target package exists at `companies/<slug>/` and validates against
  `paperclip/v1`.
- A board credential is available to the deployment: `$PAPERCLIP_BOARD_KEY`
  (`pcp_board_*`, instance-admin, expires — spike #42) or a
  `~/.paperclip/auth.json` the sync can scan. Without one the sync no-ops.
- `$PAPERCLIP_CEO_KEY` is set — the sync resolves `companyId` from it.
- Deployer has access to set `PAPERCLIP_COMPANY_TEMPLATE` in the deployment
  environment.

### Steps

1. Set `PAPERCLIP_COMPANY_TEMPLATE=<slug>` in the deployment environment (Railway
   dashboard, or `railway variables --set PAPERCLIP_COMPANY_TEMPLATE=<slug>`).
   Leave it unset to take the default (`agentsys-coala`).
2. Redeploy (`railway up`) or restart the service so the bootstrap overlay
   (`scripts/bootstrap-overlay.d/paperclip.sh`) re-runs the company sync.
3. Observe the `[company-sync]` lines in `railway logs`; confirm the active-role
   set and the `imported N active-role bundle(s)` (or `all active roles in sync`)
   line match the package manifest.

### Verification

- Board-key read — for each active role, `GET
  /api/agents/{id}/instructions-bundle/file?path=AGENTS.md` returns that role's
  `AGENTS.md` verbatim (the same readback the sync uses for idempotency), e.g.
  `curl -H "Authorization: Bearer $PAPERCLIP_BOARD_KEY"
  "$PAPERCLIP_API_URL/api/agents/<id>/instructions-bundle/file?path=AGENTS.md"`.
  Full live verification is S10 (#58).
- Agent-key probe — a heartbeat/run for each active role lands in the right
  adapter (cross-ref #58).
- The deployment behaves as the new charter prescribes (CEO greets, etc.).

---

## Forward references

| This section          | Operationalized by                                  |
| --------------------- | --------------------------------------------------- |
| Package format        | S2 (#50) — `agentsys-coala` package shell           |
| Role `AGENTS.md`      | S3–S7 (#51–#55) — CEO, CTO, Staff Eng, QA, Research |
| Active vs defined-only| S8 (#56) — sync reads the manifest                  |
| Two-plane model       | S8 (#56) — board-key import; existing adapter flip reused |
| Selector              | S9 (#57) — bootstrap reads `PAPERCLIP_COMPANY_TEMPLATE` |
| Switch runbook bodies | S9 (#57) and S10 (#58) — first live bring-up        |
| Second-package proof  | S11–S12 (#59–#60) — `default-coala` + e2e switch    |
