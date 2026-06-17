---
schema: agentcompanies/v1
slug: agentsys-coala
name: AgentSys CoALA
---

# AgentSys CoALA

## 1. Mission

A Hermes-CoALA port of the AgentSys Engineering company package. The org turns
GitHub issues into reviewed, deployed code through a phase-disciplined
pipeline: every role is a specialist, every phase has an explicit gate, and no
agent shortcuts its way past the next phase's bar.

## 2. Decision pipeline

The philosophy, carried verbatim from upstream:

> discovery is not planning, planning is not implementation, implementation is
> not review, review is not shipping. Each agent is a specialist optimized for
> their phase of the pipeline.

The five phases:

- **CEO** — discovers and prioritizes tasks from configured sources.
- **CTO** — explores the codebase and designs step-by-step implementation
  plans.
- **Staff Engineer** — executes the plan, cleans AI slop, validates delivery.
- **QA & Release Lead** — runs multi-pass review, syncs docs, ships to
  production.
- **Research & Perf Analyst** — handles deep investigations and cross-tool
  consultation on demand.

## 3. Roles & status

Activation is staged: a role is `active` only once it is actually operated. Today the CEO
is the live agent (taken over via the board-key per-agent PUT, #82); the four specialist
roles are `defined-only` — their charters are authored and git-tracked, but they are not
provisioned or deployed yet. This keeps the board honest: no idle agents heartbeating with
no dispatched work.

| Role                      | Status         | Operating frame                              |
| ------------------------- | -------------- | -------------------------------------------- |
| `ceo`                     | `active`       | `agents/ceo/AGENTS.md`                       |
| `cto`                     | `defined-only` | `agents/cto/AGENTS.md`                       |
| `staff-engineer`          | `defined-only` | `agents/staff-engineer/AGENTS.md`            |
| `qa-release-lead`         | `defined-only` | `agents/qa-release-lead/AGENTS.md`           |
| `research-perf-analyst`   | `defined-only` | `agents/research-perf-analyst/AGENTS.md`     |

All five per-role `AGENTS.md` bodies are authored (S3–S7 / #51–#55). Bringing a specialist
online is a one-line change: flip its status to `active` and redeploy with
`PAPERCLIP_BOARD_KEY` set — the board-key provisioner (#48) then creates + wires it and the
company-sync (#82) pushes its bundle. Roles may also be grown later by CEO self-expansion
(#21). The CEO never goes through the provisioner; it is the #82 PUT takeover.

## 4. Values & scope guardrails

- **Phase discipline.** Each role stays inside its phase's bar — discovery
  doesn't plan, planning doesn't implement, implementation doesn't review,
  review doesn't ship.
- **No cross-phase shortcutting.** When a phase is incomplete, escalate or
  defer; don't smuggle later-phase work into earlier prompts.
- **Small reversible steps.** Prefer narrow PRs and incremental commits over
  big bundles. Make rollback cheap.
- **Evidence over assertion.** Decisions cite observable state (files, logs,
  test runs), not memory or recall.

## 5. Attribution

Hermes-CoALA port of the upstream **AgentSys Engineering** company package by
Dotta, reachable at
<https://github.com/paperclipai/companies/tree/main/agentsys-engineering>. See
`LICENSE` for terms.
