/**
 * paperclip-hermes-remote-shim — no-fork bridge for the hermes_remote adapter.
 *
 * WHY THIS EXISTS
 * ----------------
 * ecologic-automate's `hermes-remote-paperclip-adapter` (the gateway adapter we
 * pin in docker/Dockerfile via PAPERCLIP_GATEWAY_REF) is built to be hand-patched
 * into Paperclip's BUILT-IN registry — its README tells you to edit
 * `dist/adapters/registry.js` and `@paperclipai/shared/.../constants.js`. That is
 * a fork of Paperclip, which this initiative (epic #8) exists to avoid.
 *
 * Paperclip ALSO has a no-fork external-adapter system: `POST /api/adapters/install`
 * loads a package and calls its `createServerAdapter()`, expecting back a
 * ServerAdapterModule `{ type, execute, testEnvironment, sessionCodec, ... }`.
 * The published gateway package exports those pieces RAW (no createServerAdapter),
 * so it can't be installed directly. This shim is the missing ~15 lines: it
 * re-exports the gateway adapter's functions through a `createServerAdapter()` so
 * Paperclip's external system accepts it with ZERO Paperclip source edits.
 *
 * Installing it also makes `adapterType:"hermes_remote"` pass PATCH validation:
 * Paperclip's `assertKnownAdapterType` checks the mutable adapter registry (built-ins
 * + installed externals), not a static enum — so registration alone unblocks our
 * onboarder (the same call that currently 422s "Unknown adapter type: hermes_remote").
 *
 * The returned object mirrors the gateway README's known-good register object
 * field-for-field. The gateway adapter is vendored into this package's node_modules
 * at install time (it is not on npm) — see scripts/install-paperclip-adapter.sh.
 */
import {
  execute,
  testEnvironment,
  sessionCodec,
  listSkills,
  syncSkills,
  detectModel,
} from "hermes-remote-paperclip-adapter/server";
import { agentConfigurationDoc, models } from "hermes-remote-paperclip-adapter";

export function createServerAdapter() {
  return {
    type: "hermes_remote",
    execute,
    testEnvironment,
    sessionCodec,
    listSkills,
    syncSkills,
    models,
    supportsLocalAgentJwt: true,
    agentConfigurationDoc,
    detectModel: () => detectModel(),
  };
}
