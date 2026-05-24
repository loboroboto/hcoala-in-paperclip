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
├── .dockerignore
├── .gitignore
│
├── docker/
│   └── Dockerfile                  ← Python 3.12 + Hermes + Railway CLI
│
├── scripts/
│   └── bootstrap.sh                ← idempotent setup on every container boot
│
└── hermes-config/                  ← THE ARCHITECTURE (git-tracked, durable)
    ├── AGENTS.md                   ← CoALA system prompt (memory, actions, decision cycle)
    ├── SOUL.md                     ← personality / voice
    ├── hermes.toml                 ← provider, model, paths, toolsets
    ├── mcp.json                    ← MCP grounding-action surfaces (commented examples)
    └── skills/                     ← seed procedural memory
        ├── coala-decision-cycle/   ← META — the loop, made explicit
        ├── coala-skill-induction/  ← META — how to write a skill (procedural learning)
        ├── coala-reflection/       ← META — episodic → semantic promotion
        ├── deploy-railway/         ← DOMAIN — Railway deploys
        ├── debug-incident/         ← DOMAIN — production incident triage
        └── write-quality-code/     ← DOMAIN — coding defaults
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

The agent itself can produce a CoALA self-audit when asked — it knows its
own schema.

---

## Durability Story

The foundation is **declarative and re-applyable**. A fresh Railway deploy:

1. Builds the image from `docker/Dockerfile` — installs Hermes, Railway CLI,
   and copies `hermes-config/` into `/app/`.
2. Mounts the persistent volume at `/data`.
3. Runs `scripts/bootstrap.sh` (the ENTRYPOINT), which:
   - Verifies `/app/hermes-config/` is complete.
   - Creates `/data/hermes/` subdirectories if missing.
   - Seeds `MEMORY.md` and `USER.md` if missing (idempotent — won't clobber).
   - Copies seed skills into `/data/hermes/skills/` if missing (so agent
     patches to seed skills stick; set `HERMES_FORCE_RESEED=1` to force).
   - Symlinks `~/.hermes/AGENTS.md`, `SOUL.md`, `hermes.toml`, `mcp.json`
     to the git-tracked `/app/` versions — **architecture is always fresh
     from the repo**.
   - Symlinks `~/.hermes/MEMORY.md`, `USER.md`, `skills/`, `memory.db`,
     `trajectories/` to the volume — **state persists across deploys**.
4. Execs the CMD (`hermes serve`).

**What's on the volume (mutable, persistent):**
`memory.db`, `MEMORY.md`, `USER.md`, agent-authored skills, trajectories.

**What's in git (immutable, declarative):**
the system prompt, SOUL.md, hermes.toml, mcp.json, the seed skill set.

You can wipe and redeploy Railway, lose nothing about who the agent is, and
keep everything about what it's learned.

---

## Quick Start

### 1. Push to your Railway project

```bash
git clone <this-repo>
cd <this-repo>
railway link <your-project-id>
railway up
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
| `GITHUB_TOKEN`            | Optional  | GitHub MCP                                 |
| `RAILWAY_TOKEN`           | Optional  | Railway MCP / programmatic Railway access  |
| `TELEGRAM_BOT_TOKEN`      | Optional  | Telegram gateway                           |
| `DISCORD_BOT_TOKEN`       | Optional  | Discord gateway                            |
| `SLACK_BOT_TOKEN`         | Optional  | Slack gateway                              |
| `SENTRY_AUTH_TOKEN`       | Optional  | Sentry MCP (for `debug-incident` skill)    |
| `HERMES_FORCE_RESEED`     | Optional  | Set to `1` to overwrite agent-patched seed skills on next boot |

¹ At least one provider key is required; pick the one matching `provider.name`
in `hermes.toml`.

### 4. Activate any MCP servers you want

Edit `hermes-config/mcp.json`. Examples for GitHub, Postgres, Sentry, Brave
Search, and a Railway MCP are commented out with `_disabled` markers.
Rename `_github_example` → `github` (and drop `_disabled` / `_note`) to
activate. Commit, redeploy.

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

---

## License

Apply whatever license fits your project. Hermes Agent itself is MIT.
The CoALA paper is CC-BY-4.0.
