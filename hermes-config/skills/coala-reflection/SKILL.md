---
name: coala-reflection
description: >
  Use after a failure, a surprising outcome, or at the end of a long session,
  to extract durable lessons from episodes and promote them to semantic memory.
  Implements the Reflexion-style learning pattern (CoALA §4.5, episodic →
  semantic). Trigger phrases: what did we learn, post-mortem, retro, reflect
  on, take stock, lessons learned, what went wrong.
version: 1.0.0
tags: [coala, meta, learning, semantic-memory, reflection]
---

# Reflection (CoALA §4.5, Episodic → Semantic Promotion)

Episodes are raw experience. Semantic memory is *what we now believe to be
true* because of those experiences. Reflection is the reasoning action that
bridges them — and the learning action that commits the result.

## When to Use

- After a failure, especially a non-obvious one.
- After a session containing ≥3 episodes touching the same system.
- When the user explicitly asks for a retro / post-mortem.
- At session end if the session was substantive.

## Procedure

### 1. Gather episodes
Retrieve relevant episodic memory:
- The current session's trajectory (in working memory already).
- Past episodes touching the same system, codebase, or user goal
  (`memory_search`).

### 2. Identify patterns
Reasoning action. Scan the episodes for:
- **Repeated failure modes** — the same kind of error firing more than
  once is a candidate semantic fact.
- **Surprising successes** — workarounds that worked, defaults that
  didn't apply, undocumented behaviors.
- **Stable properties** — facts about the user, infrastructure, or
  codebase that held across episodes.

### 3. Distill claims
For each pattern, write a single declarative sentence. Examples:
- "Railway's persistent volume mounts at `/data`, not `/var/data` — the
  Dockerfile must match."
- "User prefers `pnpm` over `npm` for this monorepo."
- "The staging DB rejects connections from any IP not in the Railway
  egress list; tunnel via Railway run."

Claims must be:
- **Atemporal** — true now and likely later, not "yesterday we…"
- **Falsifiable** — concrete enough to be wrong.
- **Sourced** — traceable to the episode(s) that produced them.

### 4. Resolve conflicts
Check semantic memory for existing claims on the same topic. If the new
claim contradicts an existing one, **do not overwrite silently**:
- If new evidence is stronger, revise — and log the revision.
- If unclear, write both with timestamps and surface the conflict next
  time the topic is retrieved.

### 5. Write
Learning action. Append claims to semantic memory (`MEMORY.md` or
`~/.hermes/memory/semantic/<topic>.md`). Format:
```
## <topic>
- Claim. (Source: episode <id>, <date>.)
- Claim. (Source: episodes <ids>.)
```

### 6. Optionally promote to procedural
If the reflection produced a *workflow* (not just a fact) — e.g., "when
deploy fails with X, the fix is always Y" — that's a candidate for skill
induction (see `coala-skill-induction`). Reflection produces semantic
claims; some of those claims justify procedural skills.

## Pitfalls

- **Hindsight overconfidence.** "We should have done X" written from one
  failure is often wrong. Wait for the pattern to repeat.
- **Generalizing from one episode.** N=1 is a hypothesis, not a fact.
  Label as such if writing it down: "Possible: X. Confirm if seen again."
- **Burying conflicts.** When a new claim contradicts an old one, the
  worst move is silent replacement. The conflict itself is information.
- **Reflecting on nothing.** If the session had no failures, surprises,
  or stable patterns, there's nothing to reflect on. Don't manufacture
  insights to feel productive.

## Verification

- Each new semantic claim is one sentence, declarative, sourced.
- Conflicts with prior memory are surfaced, not hidden.
- Hypotheses are labeled as such.
- The user (if present) sees a summary of what was promoted.
