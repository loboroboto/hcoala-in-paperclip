---
name: write-quality-code
description: >
  Use when writing, editing, or reviewing code in any language. Enforces
  read-before-write, surgical edits over rewrites, tests as observations, and
  explicit verification. Trigger phrases: write code, implement, refactor,
  add a function, fix this bug, edit this file, review this code, make this
  work, build a feature.
version: 1.0.0
tags: [coding, quality, defaults]
---

# Write Quality Code

Defaults for any code-writing or code-editing task. Encodes the agent's
read-before-write doctrine and treats tests as the environment's response —
grounding observations, not noise.

## When to Use

Any task that touches code. This is a near-always-on skill for the coding
agent — load it when the task involves *producing* or *modifying* code, not
when merely *explaining* code.

## Core Principles

1. **Read before write.** Never edit a file you haven't viewed in this
   session. The file is part of working memory; you don't have it until
   you've loaded it.
2. **Surgical over total.** Prefer minimal diffs to whole-file rewrites.
   Rewrites are larger blast radius and harder to review.
3. **Tests are observations.** A failing test is the environment giving
   you grounded information. Run tests; read the output; respond to it.
4. **Verify the change you made.** Don't claim a fix without evidence
   that it actually fixed the thing.
5. **Naming is the easiest part to do well and the easiest to skip.**
   Variable, function, file names — pick on first try, don't churn.

## Procedure

### 1. Observe (CoALA §4.6.1)
- View the file(s) involved.
- Run existing tests for the affected area (grounding action). Capture
  the baseline state — what passes now, what fails now.
- For a new feature: skim adjacent code to learn the codebase's
  conventions (naming, error handling, async style).

### 2. Plan
For a small change, the plan is a sentence. For a non-trivial change,
propose alternatives:
- **GROUNDING** — direct edit + test run.
- **REASONING** — sketch the change in chat first, then implement.
- For refactors: stepwise (smaller, reversible diffs) vs. atomic.

Evaluate on:
- Reversibility (smaller diff = easier revert).
- Test coverage of the affected paths (uncovered = higher risk).
- Whether the change crosses module boundaries (bigger blast radius).

### 3. Execute
- Make the edit (use `str_replace` for surgical edits; `create_file` only
  for new files).
- For each logical change, write or update a test if the codebase has
  tests. If it has no tests, mention this once and don't keep nagging.
- Run the relevant tests, not the whole suite (unless the change is
  cross-cutting).

### 4. Verify
- The tests you wrote/touched pass.
- The existing tests that were passing still pass.
- The new behavior matches the user's actual request (re-read the
  request; check for misinterpretations).
- For UI/behavior changes: run the actual flow if possible, don't just
  trust types.

### 5. Learn
If the change surfaced a non-obvious property of the codebase ("this
service uses event-loop X, can't use Y"), append to semantic memory.
If you discovered a workflow that worked well, consider a skill patch.

## Defaults by Language Family

These are conventions, not laws — defer to the codebase's existing style.

- **TypeScript/JavaScript:** strict types, `unknown` over `any`, no
  `// @ts-ignore` without a reason in a comment, async/await over raw
  Promises.
- **Python:** type hints on public APIs, `pathlib` over string paths,
  `dataclasses` or `pydantic` over dicts-as-objects.
- **Go:** errors as values, wrapped with context, `defer` for cleanup,
  no panic in libraries.
- **Rust:** `Result` over panic, lifetimes named when non-trivial,
  `clippy --all-targets` clean.
- **Shell:** `set -euo pipefail`, quote variables, prefer `[[ ]]` over
  `[ ]`, `shellcheck` clean.

## Pitfalls

- **Editing blind.** Producing a diff for a file you haven't read. The
  diff will be wrong somewhere — line numbers, imports, neighboring
  context.
- **Rewriting to fix a one-line bug.** Tempting; almost always wrong.
- **Skipping tests because "this is obviously correct."** It isn't.
- **Cargo-culting style from training data.** The codebase's conventions
  win over generic best practices. Read three nearby files before
  introducing a new pattern.
- **Claiming a fix without running it.** Verification means executing
  the changed path, not reasoning about it.
- **Drive-by reformatting.** Don't reformat unrelated code while making
  a targeted change. The diff becomes unreviewable.

## Verification

- Every edited file was viewed first this session.
- Tests for the affected area ran and pass (or, if no tests exist, the
  user is told once).
- The diff is local to the change requested — no drive-by edits.
- Naming follows codebase conventions.
- If a new file: it lives where similar files live.
