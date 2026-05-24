---
name: coala-decision-cycle
description: >
  Use when facing a non-trivial task — multi-step work, ambiguous goals, risky
  grounding actions, or anywhere the user asks you to "think through" or "plan"
  something. Walks the CoALA Observe → Plan(propose/evaluate/select) → Execute
  → Learn loop explicitly. Trigger phrases: plan this, think through, work
  through, walk me through, what would you do, how would you approach.
version: 1.0.0
tags: [coala, meta, architecture, planning]
---

# Decision Cycle (CoALA §4.6)

A bounded, explicit pass through the agent's main loop. Use this when the
task is non-trivial enough that working through the cycle out loud is worth
the tokens — typically tasks involving >2 grounding actions, irreversible
operations, or contested goals.

## When to Use

- Multi-step coding/DevOps tasks where the wrong order has real cost.
- Tasks with destructive potential (deploys, migrations, deletes).
- Tasks where the user asks for a plan rather than an answer.
- Whenever you catch yourself about to fire a grounding action without
  having thought.

## Procedure

### 1. Observe
State, in one or two sentences:
- What the user is asking.
- What changed in working memory since the last cycle (new tool output,
  new constraint, new file content).
- What is **not yet known** but needed.

### 2. Plan

#### 2a. Propose
Enumerate 2–4 candidate next actions. For each, write:
```
- [TYPE] Brief description
  Effect: what it does to the world or memory
  Reversible: yes / no / partial
```
TYPE is one of: GROUNDING, RETRIEVAL, REASONING, LEARNING.

If only one candidate is sensible, say so explicitly — don't manufacture
fake alternatives.

#### 2b. Evaluate
For each candidate, score on:
1. Correctness — does it move toward the goal?
2. Reversibility — undo cost.
3. Cost — tokens / time / money / blast radius.
4. Information value — does it teach you anything for later?

A single line per candidate is enough.

#### 2c. Select
Pick one. State why it beat the others. If two tie, prefer reversible.
If none are good enough, loop back to Propose with the evaluation as
context. Cap at two loop-backs; then ask the user.

### 3. Execute
Fire the selected action. If grounding, run the tool. If learning, write
to memory. If reasoning, produce the output. Narrate the type firing.

### 4. Learn (optional)
Before closing the cycle, ask:
- Did anything happen worth remembering as an **episode**?
  → append to episodic memory.
- Did I infer or confirm a **stable fact**?
  → append to semantic memory.
- Did I execute a **repeatable workflow** with non-obvious steps?
  → patch or author a skill (procedural memory).

If no, skip. Spurious learning is worse than no learning.

## Pitfalls

- **Phase-skipping.** Going straight from Observe to Execute. Even on
  "obvious" cycles, name your selected action's type.
- **Reasoning spirals.** Three reasoning actions in a row with no
  grounding/retrieval means you're confabulating. Break out.
- **Stealth learning.** Writing to memory without naming it as a learning
  action. Every memory write is a deliberate phase 4.
- **Cargo-cult enumeration.** Don't list four candidates when there is
  obviously one. The cycle is a tool, not a ritual.

## Verification

A well-executed cycle leaves a transcript where:
- Each phase is visible (even if one sentence).
- The selected action's CoALA type is named.
- Any memory write is explicitly flagged.
- The user can audit the architecture from the reply alone.
