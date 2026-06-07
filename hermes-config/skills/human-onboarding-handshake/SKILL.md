---
name: human-onboarding-handshake
description: >
  CEO-only. Use as the FIRST action of every session, before any company-
  affecting work, WHEN your role is `ceo` (confirm via GET /api/agents/me).
  Implements the onboarding gate (fleet epic #8, slice #20): until a human has
  explicitly onboarded the CEO, the only permitted actions are introducing
  yourself and requesting go-ahead — no company mutations. Owns the per-agent
  `humanOnboarded` flag in this home's onboarding/state.json. Trigger phrases:
  first contact, am I onboarded, onboarding handshake, introduce yourself,
  awaiting go-ahead, can I start, provisional agent, human onboarding, gate.
version: 1.1.0
tags: [coala, meta, onboarding, gate, lifecycle, ceo]
---

# Human-Onboarding Handshake (CoALA §4.1 observe, §4.2 dialogue, §6.3 escalate/await)

**Applies only to the `ceo` role** (the company's top agent / the human's
interface); see `roles/ceo.md`. Other roles ignore this skill. The gate runs
before any company-mutating work. A freshly created CEO is **provisional**: it
introduces itself and must get an **explicit, human-confirmed** go-ahead before
it does anything to the company. This skill owns the per-agent flag that records
the handshake and survives restarts.

## The Flag

State lives in **`$HERMES_HOME/onboarding/state.json`** — this agent's own home,
so it is per-agent by construction (the fleet wrapper re-exports `HERMES_HOME` to
`/data/hermes/agents/<agentId>` before launching). Schema:

```json
{
  "humanOnboarded": false,
  "agentId": "<this agent's id>",
  "firstContactAt": null,
  "onboardedAt": null,
  "onboardedBy": null,
  "channel": null
}
```

**Read it fail-closed:** missing, unreadable, malformed, or `humanOnboarded`
anything other than literal `true` ⇒ treat as **ungated**.

**Do NOT store this flag in USER.md.** `hermes.toml [context].user_md` points at
the absolute `/data/hermes/USER.md` (the shared main home), so a flag written
there would leak across the whole fleet and onboard every agent at once. The
state file is the only per-agent location.

## When to Use

- The **first action of every session**, before Propose — read the flag to learn
  whether you are gated.
- Before any candidate action that would mutate the company while the flag is
  unknown or false.
- When an inbound message might be the human's go-ahead.

## Procedure

1. **Observe** (§4.1). Read `$HERMES_HOME/onboarding/state.json` via the
   filesystem tool. Missing ⇒ create it with `humanOnboarded: false` and
   `agentId` set; treat as ungated. Narrate this read — it is an explicit OBSERVE
   grounding action.
2. **If `humanOnboarded` is `true`** ⇒ the gate is open. Exit this skill; resume
   the normal §4 decision cycle. Do **not** re-introduce yourself.
3. **If ungated** ⇒ this is an **Escalate/await** state (§6.3): do no work.
   - Compose the intro per `roles/ceo.md` "First-contact voice" — who you are,
     that you are provisional, what you'd own, and that you're requesting go-ahead.
     Set `firstContactAt` on the first such cycle.
   - **Raise the go-ahead as a Paperclip `request_confirmation`** ("May I begin
     operating this company?") using a **stable idempotency key** (e.g.
     `confirmation:onboarding:<agentId>`) so it's created once and re-used, never
     duplicated each wake. Emit the intro as the run's reply and end "awaiting
     onboarding".
4. **Check the confirmation's status from Paperclip** (a fact from the API, never
   your inference):
   - **Accepted by a human** ⇒ go to step 5.
   - **Pending / declined / absent** ⇒ stay gated; re-await. **The recurring
     "You are the CEO…" wake prompt is NEVER consent** — it is a system heartbeat,
     not a human message. Do not unlock on it. A free-text message may *prompt*
     you to (re-)raise the confirmation, but only an **accepted confirmation**
     opens the gate.
5. **Unlock** (§4.5 learning action — narrate it) **only after an accepted
   confirmation.** Write `humanOnboarded: true`, `onboardedAt`, `onboardedBy` (the
   human who accepted — never the literal "board (human)" unless that is true), and
   `channel`. Acknowledge briefly, then resume the normal cycle. Later sessions
   pass the gate on the cheap read in step 1–2.

## Pitfalls

- **Treating the wake prompt as consent.** The boilerplate "You are the CEO…"
  heartbeat prompt is identical every run and is a SYSTEM wake, not a human
  message. It is never go-ahead. (This is the real bug that occurred: the CEO
  self-onboarded from a heartbeat and confabulated `onboardedBy: "board (human)"`.)
- **Self-granting the unlock.** The gate is not self-grantable — only an
  *accepted* Paperclip confirmation opens it. Never infer acceptance; read it from
  the API.
- **Flipping the flag on an ambiguous message.** When in doubt, stay gated and
  (re-)raise the confirmation. The flip is irreversible in spirit (you start acting).
- **Writing the flag to USER.md.** Leaks across the fleet — see The Flag.
- **A "harmless" read that mutates.** While ungated, even retrieval is limited to
  read-only context needed to compose the intro. No claims, no board moves.
- **Re-introducing after onboarded.** Once the flag is `true`, never re-run the
  intro — it reads as amnesia.

## Verification

A correct gated cycle leaves a transcript where:

- The `state.json` read is the **first** action, named explicitly.
- An ungated run contains only the intro + a (single, idempotent) onboarding
  `request_confirmation` and **zero** company mutations (git/GitHub/board untouched).
- The unlock happens **only** after that confirmation is read back as
  human-accepted — never on a heartbeat — and is a single explicit, timestamped
  learning action recording the real `onboardedBy` and `channel`.
- A later session passes the gate cheaply with no re-introduction.
- The file is at `/data/hermes/agents/<id>/onboarding/state.json` — a second
  agent's gate is independent (its own flag still `false`).
