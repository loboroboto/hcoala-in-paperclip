---
name: deploy-railway
description: >
  Use when deploying a service to Railway, debugging a Railway deploy, working
  with Railway persistent volumes, or configuring Railway environment variables
  and build settings. Trigger phrases: deploy to railway, push to railway,
  railway deployment, railway volume, railway env, railway dockerfile, railway
  service, railway build failed.
version: 1.0.0
tags: [devops, deploy, railway, cloud]
platforms: [linux, macos]
---

# Deploy to Railway

Railway is the host. The agent itself runs there with a persistent volume.
This skill covers deploying *anything* to Railway — services, the agent, or
auxiliary workers.

## When to Use

- New service to be deployed to Railway.
- Existing Railway deploy failing build, crash-looping, or behaving wrong
  post-deploy.
- Volume configuration, env var setup, custom domains, service linking.

## Quick Reference

```bash
railway login                           # one-time
railway link <project-id>               # bind cwd to project
railway up                              # deploy current dir
railway logs                            # tail
railway run <cmd>                       # run cmd with project env
railway variables                       # list env
railway volume list                     # list volumes on service
```

## Procedure

### 1. Observe (CoALA §4.6.1)
Before changing anything:
```bash
railway status
railway variables --service <name>
railway logs --service <name> --deployment last
```
State in working memory: current service state, last deploy's outcome,
relevant env vars (names only — never echo secrets).

### 2. Plan
Propose candidates. Common ones:
- **GROUNDING** — `railway up` directly (fastest, no review).
- **GROUNDING** — open a PR, let CI build, merge to deploy (safer).
- **REASONING** — read the Dockerfile and config first; only deploy after
  confirming the build will work.

Evaluate by reversibility: a bad deploy on Railway is reverted via the
dashboard's "Redeploy" on the prior deployment — fast but not free. For
anything customer-facing, prefer the staged path.

### 3. Verify pre-conditions
- `Dockerfile` or `railway.toml` exists and parses.
- Required env vars are set: `railway variables --service <name>`.
- If using a volume, mount path matches what the app expects.
- If the service has health checks, confirm the endpoint exists in code.

### 4. Execute
```bash
railway up --service <name>
```
Or trigger via git push if linked to a GitHub repo.

### 5. Observe deploy
```bash
railway logs --service <name> --deployment latest --follow
```
Watch for:
- Build success (`Build successful`).
- Container start (`Starting Container`).
- Health-check pass or app's startup log line.
- Any restart loop within first 90 seconds.

### 6. Verify post-conditions
- Service status is `SUCCESS` and `RUNNING`.
- Health endpoint responds 200 (`curl https://<service>.railway.app/healthz`).
- Logs show steady state (no error spam).
- If a volume is used, write a test file and confirm it survives a restart.

### 7. Learn (CoALA §4.6.4)
If the deploy surfaced anything non-obvious (a missing env var, a wrong
mount path, a build-time vs. runtime dependency confusion), append it to
semantic memory and consider patching this skill.

## Pitfalls

- **Volume mount path mismatch.** Railway mounts at the path you configure
  in the volume settings; the Dockerfile / app must use the **same path**.
  `/data` is conventional but not magic. Mismatches manifest as "data
  disappears between deploys."
- **Build env vs. runtime env.** Variables prefixed for buildtime
  (`NIXPACKS_*`, build args) are not available at runtime, and vice versa.
- **Implicit `PORT`.** Railway injects `PORT`; the app must bind to it,
  not a hardcoded port. Bind to `0.0.0.0:$PORT`, never `127.0.0.1`.
- **Healthcheck timeout.** Default is short. Slow-starting apps need
  `healthcheckTimeout` in `railway.toml`.
- **Deploy on every git push.** If the project is linked to a branch,
  every push deploys. For an agent's own deployment, this can cause
  thrash. Use a release branch.
- **Secret echo.** Never `echo $SECRET` or log env vars in deploy
  scripts. Railway log retention is long.

## Verification

- `railway status --service <name>` shows `RUNNING`.
- HTTP health endpoint returns 200.
- No restart-loop in last 5 minutes of logs.
- If volume-backed: a sentinel file written before deploy is still
  present after.
