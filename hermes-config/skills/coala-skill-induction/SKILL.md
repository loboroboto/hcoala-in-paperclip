---
name: coala-skill-induction
description: >
  Use after completing a non-trivial workflow that you expect to repeat. Walks
  the procedure for writing a new skill (procedural-memory update, CoALA §4.5)
  or patching an existing one. Trigger phrases: save this as a skill, remember
  how to do this, that worked — keep it, write a skill for, turn this into a
  procedure, this should be reusable.
version: 1.0.0
tags: [coala, meta, learning, procedural-memory]
---

# Skill Induction (CoALA §4.5, Procedural Memory Update)

Writing or patching a skill is a **learning action** of the highest-risk
class — it modifies how the agent behaves on future cycles. This skill
governs that write.

## When to Use

Author a new skill when **all three** hold:
1. You just completed a workflow with ≥3 non-obvious steps.
2. You expect to repeat it (or something close enough that the procedure
   transfers).
3. Re-deriving it next time would cost more than reading a skill.

Patch an existing skill when:
- You just executed it and discovered a step that was wrong, missing, or
  unclear.
- A pitfall fired that wasn't documented.
- A platform or environment caveat surfaced.

**Do not** write a skill for:
- A one-off task.
- Something already covered by an existing skill (patch instead).
- A workflow you can't yet articulate without hand-waving.

## Procedure

### 1. Decide: new vs. patch
Search existing skills first (`skill_manage list` or skim
`~/.hermes/skills/`). If something is close, patch it — don't fork.

### 2. Name and scope
- `name`: kebab-case, ≤64 chars, action-oriented (`deploy-fly`, not
  `fly-stuff`).
- `description`: a single dense paragraph including 3–5 **trigger phrases**
  the user might say. The description is how future-you finds this skill.

### 3. Structure the body
Use the standard sections:
- **When to Use** — conditions for activation, in plain prose.
- **Procedure** — numbered, executable steps. Each step should be either
  a grounding action, a retrieval, or a checkpoint.
- **Pitfalls** — failure modes you actually hit or expect to hit. Skip
  generic warnings.
- **Verification** — how you know it worked. Concrete checks, not vibes.

### 4. Write the SKILL.md
- Frontmatter fields: `name`, `description`, `version` (semver), `tags`.
- Optional: `platforms` if the skill is OS-specific,
  `required_environment_variables` if it needs secrets.
- Body in markdown, target <5000 tokens.

### 5. Verify before committing
- Read it back. Would a fresh session execute it correctly?
- Are all tools/commands named exactly? No vague "run the migration."
- Are pitfalls grounded in *what actually happened*, not theory?

### 6. Persist
- Author skills go under `~/.hermes/skills/<skill-name>/SKILL.md`.
- If this skill is portable / shareable, mirror it into the git-tracked
  `hermes-config/skills/` so a fresh deploy picks it up via bootstrap.

### 7. Log the learning action
After writing, append an episodic note: "Authored skill X on [date]
because [trigger]. Verification: [how checked]." This makes the
procedural mutation auditable.

## Pitfalls

- **Premature induction.** Writing a skill from one execution captures
  noise as signal. Wait for the second execution; let it inform the first.
- **Description without triggers.** Skills without natural-language
  trigger phrases in the description don't get retrieved. The
  `description` is the retrieval key — phrase it from the user's POV.
- **Encyclopedic skills.** A skill is a procedure, not a tutorial. Cut
  background context; link to it if needed.
- **Untested patches.** Patching a working skill without running the new
  version risks breaking future cycles. Verify before saving.

## Verification

- The new/patched SKILL.md parses (frontmatter valid YAML, body markdown).
- Running through the procedure mentally on a fresh session produces the
  same outcome you just achieved.
- The description contains ≥3 trigger phrases.
- An episodic note records the procedural mutation.
