# Hermes Agent — CoALA-Aligned Foundation

A Hermes Agent ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)) deployment whose
foundational architecture is explicitly aligned with the **Cognitive
Architectures for Language Agents (CoALA)** framework — Sumers, Yao,
Narasimhan & Griffiths, [arXiv:2309.02427v3](https://arxiv.org/html/2309.02427v3).

Hermes provides the substrate (skills, memory, tools, MCP, messaging
gateways). CoALA provides the schema imposed on that substrate. The pairing
is durable, git-tracked, and reconstitutes a fresh Railway deploy into the
same architecture on every boot.

---

## Repository Layout

```
.
├── README.md                       ← you are here
├── railway.toml                    ← Railway build/deploy config
├── .env.example                    ← committed template; copy to .env for local dev
├── .dockerignore
├── .gitignore
│
├── docker/
│   └── Dockerfile                  ← uv + tini + pinned Hermes + pre-built ui-tui + Railway CLI
│
├── scripts/
│   └── bootstrap.sh                ← idempotent setup on every container boot
│
└── hermes-config/                  ← THE ARCHITECTURE (git-tracked, durable)
    ├── AGENTS.md                   ← CoALA system prompt (memory, actions, decision cycle, group ops)
    ├── SOUL.md                     ← personality / voice
    ├── hermes.toml                 ← provider, model, paths, toolsets, [peers], [channels]
    ├── mcp.json                    ← MCP grounding-action surfaces (github live; others commented)
    └── skills/                     ← seed procedural memory
        ├── coala-decision-cycle/      ← META — the loop, made explicit
        ├── coala-skill-induction/     ← META — how to write a skill (procedural learning)
        ├── coala-reflection/          ← META — episodic → semantic promotion
        ├── group-agent-coordination/  ← META — peer claims, hand-offs, conflict resolution
        ├── channel-aware-messaging/   ← META — pick the right channel; respect etiquette
        ├── deploy-railway/            ← DOMAIN — Railway deploys
        ├── debug-incident/            ← DOMAIN — production incident triage
        ├── github-projects-ops/       ← DOMAIN — issues, PRs, projects, milestones
        └── write-quality-code/        ← DOMAIN — coding defaults
```

---

## CoALA → Hermes Mapping

This is the foundational mapping. Each CoALA primitive (left) is realized by
a specific Hermes mechanism (right).

| CoALA primitive (§ in paper)               | Hermes substrate                              | Where it lives                                |
|--------------------------------------------|-----------------------------------------------|-----------------------------------------------|
| **Working memory** (§4.1)                  | Conversation context + context files          | runtime; `AGENTS.md` + `SOUL.md` always loaded |
| **Episodic memory** (§4.1)                 | FTS5-indexed session history (SQLite)         | `/data/hermes/memory.db`                       |
| **Semantic memory** (§4.1)                 | Curated facts file + Honcho user model        | `/data/hermes/MEMORY.md`, `USER.md`            |
| **Procedural memory — implicit** (§4.1)    | LLM weights                                   | provider (Nous Portal / OpenRouter / etc.)     |
| **Procedural memory — explicit** (§4.1)    | Skills + AGENTS.md + decision scaffolds       | `/app/hermes-config/skills/` + `/data/hermes/skills/` |
| **Grounding actions** (§4.2)               | Built-in tools (shell, fs, web, git, etc.) + MCP servers | `hermes.toml` toolsets + `mcp.json`     |
| **Retrieval actions** (§4.3)               | `memory_search`, skill index, context loading | runtime                                        |
| **Reasoning actions** (§4.4)               | LLM calls scaffolded by AGENTS.md             | runtime                                        |
| **Learning actions** (§4.5)                | Memory writes + `skill_manage` for skill author/patch | runtime, persists to `/data`            |
| **Decision cycle** (§4.6 propose/eval/select) | Encoded in `AGENTS.md` §4 + `coala-decision-cycle` skill | both prompt-level and skill-level     |
| **Multi-agent grounding** (§4.2 dialogue, other agents) | Peer registry + channel registry + MCP transports + peer semantic model | `hermes.toml [peers]` / `[channels]` + `mcp.json` + `/data/hermes/PEERS.md` |
| **Group decision cycle** (§4.6 in groups)  | Local cycle with group-coherence criterion + coordination primitives | `AGENTS.md` §6 + `group-agent-coordination` skill |

The agent itself can produce a CoALA self-audit when asked — it knows its
own schema.

---

## Durability Story

The foundation is **declarative and re-applyable**. A fresh Railway deploy:

1. Builds the image from `docker/Dockerfile` — installs Hermes, Railway CLI,
   and copies `hermes-config/` into `/app/`.
2. Mounts the persistent volume at `/data`.
3. Runs `scripts/bootstrap.sh` (the ENTRYPOINT, wrapped in `tini` as PID 1
   so MCP stdio servers and other subprocess fanout get reaped cleanly and
   `SIGTERM` propagates through the whole process group), which:
   - Verifies `/app/hermes-config/` is complete.
   - Creates the full set of `/data/hermes/` subdirectories hermes expects
     (cron, sessions, logs, pairing, hooks, image_cache, audio_cache,
     workspace, plans, home, plus our memory/skills/trajectories).
   - Clears any stale `gateway.pid` lockfile from a prior container.
   - Bootstraps OAuth tokens to `/data/hermes/auth.json` if
     `HERMES_AUTH_JSON_BOOTSTRAP` is set and no file exists yet.
   - Seeds `MEMORY.md`, `USER.md`, and `PEERS.md` if missing (idempotent —
     won't clobber).
   - Copies seed skills into `/data/hermes/skills/` if missing (so agent
     patches to seed skills stick; set `HERMES_FORCE_RESEED=1` to force).
   - Symlinks `~/.hermes/AGENTS.md`, `SOUL.md`, `hermes.toml`, `mcp.json`
     to the git-tracked `/app/` versions — **architecture is always fresh
     from the repo**.
   - Symlinks `~/.hermes/MEMORY.md`, `USER.md`, `PEERS.md`, `skills/`,
     `memory.db`, `trajectories/`, `auth.json`, and the hermes-native
     subdirs to the volume — **state persists across deploys**.
4. Execs the CMD (`hermes serve`).

The image bakes a **pinned hermes-agent version** (via the `HERMES_REF`
build arg, default `v2026.5.16`) instead of `pip install` against the
latest, so rebuilds are reproducible. Bump it deliberately when you want
a new upstream.

**What's on the volume (mutable, persistent):**
`memory.db`, `MEMORY.md`, `USER.md`, `PEERS.md`, agent-authored skills,
trajectories.

**What's in git (immutable, declarative):**
the system prompt, SOUL.md, hermes.toml (including peer + channel
registries), mcp.json, the seed skill set.

You can wipe and redeploy Railway, lose nothing about who the agent is, and
keep everything about what it's learned.

---

## Group Operation

The agent is built to operate alongside other independent agents — GitHub
review bots, paperclip_ai-style coordinators, multica swarms, anything that
speaks via MCP or a custom adapter. The architecture (`AGENTS.md` §6) treats
this as three orthogonal concerns:

| Concept    | What it is                            | Where it lives                                              |
|------------|---------------------------------------|-------------------------------------------------------------|
| **Peers**  | *Who* the other agents are            | `hermes-config/hermes.toml [[peers.peer]]` + `/data/hermes/PEERS.md` (experience-grounded model) |
| **Channels** | *Where* messages flow               | `hermes-config/hermes.toml [[channels.channel]]` (kind / direction / visibility / etiquette) |
| **Transports** | *How* messages get there          | `hermes-config/mcp.json` (MCP servers; the only realization that requires code) |

A peer can be reachable through 0..N channels via 1..N transports. The agent
reasons over peers + channels at the cognitive layer; transports stay
invisible to the decision cycle.

### Adding a new group-agent platform

Wiring a new platform (e.g., activating `paperclip_ai`) is a three-edit
operation, no code changes:

1. **Transport** — in `mcp.json`, rename `_paperclip_ai_example` → `paperclip_ai`, drop `_disabled` / `_note`, fill in real `command` / `args` / `env`.
2. **Peer(s)** — in `hermes.toml`, uncomment the `[[peers.peer]]` block for paperclip-ai (or add new ones for each agent identity you collaborate with on that platform), set `transport_ref = "paperclip_ai"`.
3. **Channel(s)** — in `hermes.toml`, uncomment / add `[[channels.channel]]` entries with `transport_ref = "paperclip_ai"` and appropriate `kind` / `visibility` / `etiquette`.

Commit, redeploy. The `group-agent-coordination` and `channel-aware-messaging` skills auto-cover the new surface; no per-platform skill required unless the platform has domain-specific operations (e.g., `github-projects-ops` exists because GitHub has issues / projects / milestones as distinct entities, not because GitHub is special).

### Live by default

- **GitHub** (`github` MCP) — `github-issue`, `github-pr-comments`, and `github-project-events` channels are pre-wired. Set `GITHUB_TOKEN` to activate.

### Stubbed (uncomment to wire)

- **paperclip_ai** — group-agent coordination platform.
- **multica** — multi-agent swarm.
- **generic agent bridge** — protocol-agnostic shape for platforms that aren't MCP-native.

---

## Quick Start

### 1. Push to your Railway project

```bash
git clone <this-repo>
cd <this-repo>
railway link <your-project-id>
railway up
```

For **local dev** (`docker run` against a named volume) instead of Railway:
copy `.env.example` to `.env`, fill in real values, then:

```bash
docker build -t hermes-coala -f docker/Dockerfile .
docker run --rm -it --env-file .env -v hermes-data:/data hermes-coala
```

### 2. Configure the volume in the Railway dashboard

- **Mount path:** `/data` (must match `hermes.toml`'s paths)
- **Size:** ≥ 1GB (memory.db + skills + trajectories grow over time)

### 3. Set required env vars

In the Railway dashboard:

| Variable                  | Required? | Purpose                                    |
|---------------------------|-----------|--------------------------------------------|
| `NOUS_API_KEY`            | Yes¹      | LLM provider (Nous Portal)                 |
| `OPENROUTER_API_KEY`      | Yes¹      | Alternative provider                       |
| `OPENAI_API_KEY`          | Yes¹      | Alternative provider                       |
| `GITHUB_TOKEN`            | Optional² | GitHub MCP — live by default; required for the `github-*` channels and the `github-projects-ops` skill |
| `RAILWAY_TOKEN`           | Optional  | Railway MCP / programmatic Railway access  |
| `TELEGRAM_BOT_TOKEN`      | Optional  | Telegram gateway                           |
| `DISCORD_BOT_TOKEN`       | Optional  | Discord gateway                            |
| `SLACK_BOT_TOKEN`         | Optional  | Slack gateway                              |
| `SENTRY_AUTH_TOKEN`       | Optional  | Sentry MCP (for `debug-incident` skill)    |
| `PAPERCLIP_AI_TOKEN`      | Optional  | paperclip_ai peer-agent platform (stub — uncomment in `mcp.json`) |
| `MULTICA_TOKEN`           | Optional  | multica peer-agent swarm (stub — uncomment in `mcp.json`)         |
| `HERMES_AUTH_JSON_BOOTSTRAP` | Optional | Contents of a locally-generated `~/.hermes/auth.json`. Written to `/data/hermes/auth.json` on first boot, then refreshed in place. Use for OAuth-based providers (xAI Grok SuperGrok, Gemini CLI, Qwen OAuth, Claude Code) — avoids the interactive device-flow on first run. |
| `HERMES_FORCE_RESEED`     | Optional  | Set to `1` to overwrite agent-patched seed skills on next boot |

¹ At least one provider key is required; pick the one matching `provider.name`
in `hermes.toml`.

² Without `GITHUB_TOKEN`, the GitHub MCP errors at call-time but doesn't
crash the agent — the `github-*` channels simply become unreachable and
the agent will say so on attempted use.

### 4. Activate any MCP servers you want

Edit `hermes-config/mcp.json`. The `github` entry is **live by default**
(set `GITHUB_TOKEN` to use it). Other entries — Postgres, Sentry, Brave
Search, Railway, paperclip_ai, multica, and a generic agent-bridge
template — are commented out with `_disabled` markers. Rename
`_foo_example` → `foo` (and drop `_disabled` / `_note`) to activate. For
peer-agent platforms, also uncomment the matching `[[peers.peer]]` and
`[[channels.channel]]` blocks in `hermes-config/hermes.toml` — see
[Group Operation](#group-operation). Commit, redeploy.

### 5. Talk to it

If you enabled a messaging gateway, message the agent there. Otherwise, the
serve mode exposes a CLI/API endpoint per the Hermes docs. For interactive
debugging:

```bash
railway run -- hermes --tui
```

---

## Modifying the Architecture

Because the architecture is git-tracked, all changes are PR-reviewable.

| To change…                                  | Edit                                          |
|---------------------------------------------|-----------------------------------------------|
| How the agent thinks (memory schema, action types, decision cycle) | `hermes-config/AGENTS.md` |
| How the agent talks (voice, tone, posture)  | `hermes-config/SOUL.md`                        |
| Which model, which provider                 | `hermes-config/hermes.toml` `[model]` / `[provider]` |
| Which tools are enabled                     | `hermes-config/hermes.toml` `[tools]`          |
| External grounding surfaces (APIs, services) | `hermes-config/mcp.json`                      |
| Group-agent peers and channels              | `hermes-config/hermes.toml` `[peers]` / `[channels]` (see [Group Operation](#group-operation)) |
| Seed procedural knowledge                   | `hermes-config/skills/<name>/SKILL.md` (add/edit) |
| Where state persists                        | `hermes-config/hermes.toml` paths + `bootstrap.sh` symlinks |

Commit, push, redeploy. Bootstrap is idempotent — re-running it never
destroys volume state.

---

## Verifying CoALA Alignment

Ask the agent (in a session):

> Walk me through your architecture. Name each memory module, where it
> lives, and the four action types. Then describe your decision cycle.

A well-aligned agent will reproduce §2 and §4 of `AGENTS.md` in its own
words, with CoALA section references. If it can't, the system prompt isn't
loading — check that `~/.hermes/AGENTS.md` symlinks correctly to
`/app/hermes-config/AGENTS.md`.

And for group-operation alignment:

> You're about to post a comment on a GitHub PR where another agent has
> a draft open on the same file. Walk me through your decision cycle.
> Name the channel, the peer, the coordination primitive, and which
> AGENTS.md section governs the call.

A well-aligned agent will name the `github-pr-comments` channel
(`kind=group`, `visibility=public`), identify the peer from `PEERS.md`,
recognize the draft as a peer claim, choose between *defer* and
*negotiate* as the coordination primitive, and cite AGENTS.md §6.3.

---

## License

Apply whatever license fits your project. Hermes Agent itself is MIT.
The CoALA paper is CC-BY-4.0.
