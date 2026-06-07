# Fleet registry (`fleet/agents.yaml`)

The declarative, git-tracked **desired state** for running this repo's CoALA Hermes
agents as an external fleet inside Paperclip (epic #8). The onboarder/reconciler
(slice #14) reads this at `/app/fleet/agents.yaml` (baked in via
`COPY fleet /app/fleet`) and converges each listed agent into Paperclip's live
state over the CEO agent key. Paperclip's Postgres holds the live state; this file
is the durable source that re-converges after a wipe once the adapter is re-approved.

This slice (#13) ships **data only** — no code reads it yet. The reconcile logic is #14.

## Structure

```yaml
defaults:        # applied to every agent unless the agent overrides the same key
  ...
companies:
  - id: <paperclip-company-uuid>
    ceo: true    # first-company marker
    agents:
      - name: <label>
        existingId: <paperclip-agent-uuid>   # onboard an already-created agent
        role: ceo|worker
        hermesHome: /data/hermes/agents/<agentId>
```

`defaults` provide fleet-wide values; any field set on an individual agent overrides
the default of the same name (agent-level wins).

## Field → Paperclip config mapping

The onboarder maps each resolved field (default merged with agent override) onto the
Paperclip `hermes_remote` `adapterConfig` / `runtimeConfig` shape (camelCase keys
match 1:1):

| Registry field        | Maps to                              | Notes |
|-----------------------|--------------------------------------|-------|
| `remoteRunnerUrl`     | `adapterConfig.remoteRunnerUrl`      | shared runner (Topology 1) or per-agent override (Topology 2) |
| `runnerAuthTokenEnv`  | `adapterConfig.runnerAuthToken`      | names an **env var**; the onboarder writes its literal VALUE (no `{{}}` templating in Paperclip) |
| `paperclipApiUrl`     | API base the onboarder calls         | `PATCH /api/agents/{id}` etc., over the IPv6 private net |
| `hermesHome`          | `adapterConfig.env.HERMES_HOME`      | per-agent CoALA home; the runner's `hermes-fleet-entry.sh` seeds + isolates it (#11) |
| `persistSession`      | `adapterConfig.persistSession`       | |
| `timeoutSec`          | `adapterConfig.timeoutSec`           | |
| `model`               | `adapterConfig.model`                | adapter passes it to `hermes chat --model`; **overrides** the per-agent `~/.hermes/config.yaml` default (seeded from hermes's upstream `cli-config.yaml.example`) |
| `heartbeat`           | `runtimeConfig.heartbeat`            | `{ enabled, intervalSec }` |
| `existingId`          | the agent the reconcile targets      | onboard a pre-existing agent (vs. create); **overrides** `resolveCeoFromKey` |
| `resolveCeoFromKey`   | (company-level) CEO id resolution    | when `true`, a `role: ceo` agent with no `existingId` is resolved from the CEO key via `GET /api/agents/me` — no hardcoded id |
| `role: ceo` + `ceo:`  | CEO special-casing                   | the CEO is gated behind the human handshake (#20) before it may act on the company |

The resulting target config matches epic #8's "Target `hermes_remote` agent config".

## Fresh deploy / no hardcoded ids

A pinned `company.id` + agent `existingId` are specific to one Paperclip instance — on a
freshly deployed instance Paperclip generates new ids, so a pin would 404 (`absent`) and
nothing onboards. `resolveCeoFromKey: true` removes that coupling: the onboarder calls
`GET /api/agents/me` with the CEO bearer key to learn the CEO agent's `id` and `companyId`,
then onboards that agent — so the **key alone** bootstraps the CEO.

Resolution order for a `role: ceo` agent:

1. `existingId` present → use it verbatim (a per-instance pin; always wins).
2. else `resolveCeoFromKey: true` → resolve from the key: `me.id` when `me.role == "ceo"`,
   else the `chainOfCommand` entry with `role == "ceo"`, else the company agent list. If the
   company `id` is pinned it must match the key's company (cross-company onboard is refused);
   otherwise the resolved `companyId` is adopted.
3. else → skipped (agent creation is slice #21).

A failed resolution (key down, no CEO found, company mismatch) is non-fatal — the loop reports
`unresolved` and backs off, same as `waiting`/`absent`. For a fresh template deploy, ship this
file with the pins removed; the running instance keeps its pin until you deliberately migrate.
The CEO *creating subordinate agents* under itself is slice #21.

## Topologies

- **Topology 1 (default)** — one shared runner service, many isolated per-agent
  homes. Every agent inherits `defaults.remoteRunnerUrl`
  (`hermes-interprets-coala.railway.internal:8788/run`) and is isolated only by its
  own `hermesHome` (`/data/hermes/agents/<agentId>`). No runner code needed (#11).
- **Topology 2 (scale-out)** — one runner service per agent. An agent sets its own
  `remoteRunnerUrl`; the onboarder treats it as a distinct runner. See the
  commented `scout` example in `agents.yaml`.

## Conventions

- `runnerAuthTokenEnv` is an env-var **name**, never a secret value or a `{{...}}`
  reference — Paperclip resolves nothing at runtime.
- UUIDs come from the live Paperclip instance (the CEO agent
  `4c4080c6-8e95-4637-9b3e-45df1b3a0d15` in company
  `cf698121-875b-45b1-b6e3-3832b8a9af51` / LOB).
- Editing the fleet = editing this file; the onboarder reconciles the change. Runtime
  growth driven by the CEO itself is slice #21.
