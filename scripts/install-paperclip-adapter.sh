#!/usr/bin/env bash
# install-paperclip-adapter.sh — register the hermes_remote adapter into a running
# Paperclip WITHOUT forking Paperclip (fleet epic #8, slice #12).
#
# Run this INSIDE the Paperclip service:
#     railway ssh --service Paperclip 'bash -s' < scripts/install-paperclip-adapter.sh
# (or paste it into a railway ssh session). It needs only `node`, `tar`, `gzip`,
# and `npm` — all present in the Paperclip image; `git`/`curl` are NOT required.
#
# What it does, all on the durable /paperclip volume so it survives redeploys:
#   1. downloads + extracts the pinned gateway adapter (node https → tar)
#   2. installs its one runtime dep (@paperclipai/adapter-utils, from npm)
#   3. writes the paperclip-hermes-remote-shim package (mirrors
#      fleet/paperclip-adapter-shim/) and vendors the gateway adapter into its
#      node_modules
#   4. load-verifies the shim exactly as Paperclip's loader will
#   5. registers it in /paperclip/adapter-plugins.json (the external-adapter store
#      Paperclip's init pass reads on startup)
# Then RESTART Paperclip so the init pass loads it (the script prints the command).
#
# WHY a shim: the published gateway adapter exports raw functions meant to be
# hand-patched into Paperclip's built-in registry (a fork). Paperclip's no-fork
# external system instead loads a package whose createServerAdapter() returns the
# adapter module — so this shim bridges the two. See fleet/paperclip-adapter-shim/README.md.
#
# Idempotent: safe to re-run (rebuilds the staged dir, upserts the store record).
set -euo pipefail

GATEWAY_REPO="ecologic-automate/paperclip-hermes-gateway"
# Pin == docker/Dockerfile PAPERCLIP_GATEWAY_REF (keep in lockstep on bumps).
GATEWAY_SHA="${PAPERCLIP_GATEWAY_REF:-3604960acbc4b00d2cfd01bbabe056fbb252405f}"
GATEWAY_VERSION="0.2.3"
PAPERCLIP_HOME="${PAPERCLIP_HOME:-/paperclip}"

SRC_DIR="$PAPERCLIP_HOME/adapters-src"
SHIM_DIR="$SRC_DIR/paperclip-hermes-remote-shim"
STORE="$PAPERCLIP_HOME/adapter-plugins.json"
OWNER="$(stat -c '%U:%G' "$PAPERCLIP_HOME" 2>/dev/null || echo paperclip:paperclip)"

log() { printf '[install-adapter] %s\n' "$*" >&2; }

command -v node >/dev/null || { log "FATAL: node not found"; exit 1; }
command -v tar  >/dev/null || { log "FATAL: tar not found";  exit 1; }
[[ -d "$PAPERCLIP_HOME" ]] || { log "FATAL: $PAPERCLIP_HOME not found (is this the Paperclip service?)"; exit 1; }

log "gateway $GATEWAY_REPO @ $GATEWAY_SHA  →  $PAPERCLIP_HOME"
rm -rf "$SHIM_DIR" "$SRC_DIR/gw"
mkdir -p "$SHIM_DIR" "$SRC_DIR/gw"

# 1. download + extract the pinned gateway adapter (no git/curl in the image)
cat > "$SRC_DIR/.dl.mjs" <<'DLEOF'
import https from "node:https"; import fs from "node:fs";
const [, , sha, out] = process.argv;
const url = `https://codeload.github.com/ecologic-automate/paperclip-hermes-gateway/tar.gz/${sha}`;
await new Promise((res, rej) => {
  https.get(url, (r) => {
    if (r.statusCode !== 200) { rej(new Error("HTTP " + r.statusCode)); return; }
    const f = fs.createWriteStream(out); r.pipe(f); f.on("finish", () => f.close(res));
  }).on("error", rej);
});
console.error("downloaded", fs.statSync(out).size, "bytes");
DLEOF
node "$SRC_DIR/.dl.mjs" "$GATEWAY_SHA" "$SRC_DIR/gw.tgz"
tar -xzf "$SRC_DIR/gw.tgz" -C "$SRC_DIR/gw" --strip-components=1
rm -f "$SRC_DIR/gw.tgz" "$SRC_DIR/.dl.mjs"

GW_ADAPTER="$SRC_DIR/gw/adapter"
[[ -f "$GW_ADAPTER/dist/index.js" ]] || { log "FATAL: gateway adapter dist/index.js missing"; exit 1; }

# 2. install the gateway adapter's runtime dep (@paperclipai/adapter-utils, on npm)
( cd "$GW_ADAPTER" && npm install --omit=dev --ignore-scripts --no-audit --no-fund >/dev/null 2>&1 )
log "gateway adapter staged + dependency installed"

# 3. write the shim package (KEEP IN SYNC with fleet/paperclip-adapter-shim/)
cat > "$SHIM_DIR/package.json" <<'PKGEOF'
{
  "name": "paperclip-hermes-remote-shim",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "description": "createServerAdapter() wrapper registering hermes-remote-paperclip-adapter into Paperclip's no-fork external-adapter system.",
  "exports": { ".": "./index.js" },
  "dependencies": { "hermes-remote-paperclip-adapter": "*" }
}
PKGEOF
cat > "$SHIM_DIR/index.js" <<'IDXEOF'
// paperclip-hermes-remote-shim — no-fork bridge. Mirrors fleet/paperclip-adapter-shim/index.js.
// Wraps the gateway adapter's raw exports in createServerAdapter() so Paperclip's
// external-adapter system loads it with zero Paperclip source edits. Object fields
// mirror the gateway README's known-good register object.
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
IDXEOF

# 4. vendor the gateway adapter (with its node_modules) into the shim
mkdir -p "$SHIM_DIR/node_modules"
mv "$GW_ADAPTER" "$SHIM_DIR/node_modules/hermes-remote-paperclip-adapter"
rm -rf "$SRC_DIR/gw"

# 5. load-verify exactly as Paperclip's plugin-loader does (import → createServerAdapter → type)
cat > "$SRC_DIR/.verify.mjs" <<'VEOF'
const dir = process.argv[2];
const m = await import(dir + "/index.js");
if (typeof m.createServerAdapter !== "function") throw new Error("no createServerAdapter export");
const a = m.createServerAdapter();
if (!a || a.type !== "hermes_remote") throw new Error("bad type: " + (a && a.type));
for (const k of ["execute", "testEnvironment"]) {
  if (typeof a[k] !== "function") throw new Error("missing function: " + k);
}
if (!a.sessionCodec) throw new Error("missing sessionCodec"); // an object (encode/decode), not a fn
console.error("verify OK: type=" + a.type + ", fields=" + Object.keys(a).join(","));
VEOF
node "$SRC_DIR/.verify.mjs" "$SHIM_DIR"
rm -f "$SRC_DIR/.verify.mjs"

# 6. upsert the external-adapter store record (init pass reads this on startup)
cat > "$SRC_DIR/.register.mjs" <<'REOF'
import fs from "node:fs";
const [, , store, localPath, version] = process.argv;
let arr = [];
try { arr = JSON.parse(fs.readFileSync(store, "utf-8")); } catch { /* new store */ }
if (!Array.isArray(arr)) arr = [];
arr = arr.filter((r) => r && r.type !== "hermes_remote");
arr.push({
  packageName: "hermes-remote-paperclip-adapter",
  localPath,
  version,
  type: "hermes_remote",
  installedAt: new Date().toISOString(),
});
fs.writeFileSync(store, JSON.stringify(arr, null, 2) + "\n");
console.error("wrote " + store + " (" + arr.length + " record(s))");
REOF
node "$SRC_DIR/.register.mjs" "$STORE" "$SHIM_DIR" "$GATEWAY_VERSION"
rm -f "$SRC_DIR/.register.mjs"

chown -R "$OWNER" "$SRC_DIR" "$STORE" 2>/dev/null || true

log "DONE — hermes_remote registered. Restart Paperclip so its init pass loads it:"
log "    railway restart --service Paperclip"
log "Verify after restart:"
log "    railway logs --service Paperclip | grep -i 'external adapter'"
