# paperclip-hermes-remote-shim

A ~15-line **no-fork** bridge that registers the `hermes_remote` adapter into a
running Paperclip — the single board gate of the fleet (epic #8, slice #12).

## The problem it solves

`hermes-remote-paperclip-adapter@0.2.3` (ecologic-automate/paperclip-hermes-gateway,
pinned by `PAPERCLIP_GATEWAY_REF` in `docker/Dockerfile`) is **not** a drop-in for
Paperclip's external-adapter system:

| | What Paperclip's no-fork system wants | What the gateway package ships |
|---|---|---|
| Entry (`exports["."]`) | a module exporting **`createServerAdapter()`** returning `{type, execute, testEnvironment, sessionCodec, …}` | `{type, label, models, agentConfigurationDoc}` — **no** `createServerAdapter` |
| `./server` | — | raw `execute`, `testEnvironment`, `sessionCodec`, `detectModel`, `listSkills`, `syncSkills` |

The gateway README's official integration is to **patch Paperclip's source**
(`dist/adapters/registry.js` + `@paperclipai/shared` constants) — i.e. a fork. This
shim instead wraps the raw exports in a `createServerAdapter()` so Paperclip's
`POST /api/adapters/install` loads it unmodified.

## Why install (not source-patch) is enough

Paperclip validates `adapterType` via `assertKnownAdapterType` →
`findServerAdapter(type)` against the **mutable** registry (built-ins + installed
externals), *not* a static `AGENT_ADAPTER_TYPES` enum (see `server/src/routes/agents.ts`
and the `adapter-plugin.md` design note). So installing this shim:

1. registers `hermes_remote` in the registry (runtime `execute` path), **and**
2. makes `PATCH /api/agents/{id}` with `adapterType:"hermes_remote"` pass validation
   — the exact 422 (`Unknown adapter type: hermes_remote`) our onboarder keys on
   flips to success.

No `@paperclipai/shared` patch needed (that was the obsolete v0.1.1 manual path).

## How it's installed

`scripts/install-paperclip-adapter.sh` (run inside the Paperclip service via
`railway ssh`) assembles this on the durable `/paperclip` volume:

```
/paperclip/adapters-src/paperclip-hermes-remote-shim/
  package.json        # this dir (copied by the script)
  index.js
  node_modules/
    hermes-remote-paperclip-adapter/        # pinned gateway adapter @ PAPERCLIP_GATEWAY_REF
      node_modules/@paperclipai/adapter-utils/   # the one runtime dep (from npm)
```

then registers it in `/paperclip/adapter-plugins.json` (the external-adapter store
Paperclip's init pass reads on startup) and restarts Paperclip. Because the store
+ files live on the persistent volume, the registration survives redeploys; after a
full volume wipe an operator just reruns the script (the fleet's "re-converge after
wipe" story, slice #15).

The gateway adapter is **vendored at install time**, not committed here — we pin it
by the same SHA as the runner in `docker/Dockerfile`. Keep `index.js` in sync with
the inline copy in `scripts/install-paperclip-adapter.sh`.

## Verifying

The install script load-checks the shim exactly as Paperclip's loader does
(`import()` → `createServerAdapter()` → assert `type==="hermes_remote"` and the
function fields exist). After restart:

```bash
railway logs --service Paperclip | grep -i 'external adapter'   # "Loading external adapter package"
```
and the onboarder stops logging `waiting for board adapter approval`.
