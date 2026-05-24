---
name: debug-incident
description: >
  Use when a production system is broken, degraded, or behaving unexpectedly —
  errors in logs, alerts firing, users reporting issues, deploys that worked
  in staging but failed in prod. Walks an evidence-first triage flow. Trigger
  phrases: production is down, prod broken, incident, outage, users reporting,
  alert firing, debug this, something's wrong, errors in production, page from.
version: 1.0.0
tags: [devops, debugging, incident, sre]
---

# Debug a Production Incident

Incidents are an environment shouting at you. The job is to **observe before
hypothesizing**. Most incidents are misdiagnosed because someone reached for
a guess before reading the logs.

## When to Use

- Alert fired, user reported issue, deploy failed, service degraded.
- A bug that "shouldn't be possible" given the code.
- Something works locally but not in production.

## Procedure

### 1. Observe — gather, don't theorize (CoALA §4.6.1)
**Grounding actions only at this stage.** No hypotheses yet.

Collect:
- Recent logs (last 15 min and around the first error).
- Recent deploys (`git log --since="2 hours ago"`, deploy platform's
  deploy history).
- Metrics if available (error rate, latency, CPU/memory).
- The exact user-facing symptom (one sentence, no interpretation).
- The blast radius (one user? all users? one region? one endpoint?).

Write the raw facts into working memory. Do **not** start fixing yet.

### 2. Retrieve prior incidents (CoALA §4.3)
`memory_search` for past incidents with similar symptoms. If this system
has failed this way before, the prior episode is gold.

### 3. Plan — generate hypotheses
Now reason. Propose 2–4 candidate causes ranked by prior probability:
- Did anything change recently? (Deploy, config, infra, dependency.)
  Recent change is the #1 prior.
- Is it a known failure mode for this stack?
- Is it correlated with a specific input, user, or region?

For each hypothesis, state the **cheapest grounding action** that would
confirm or refute it.

### 4. Execute — confirm before fixing
Run the diagnostic action for the top hypothesis. **Do not** apply a fix
yet. The goal at this stage is *evidence*, not resolution.

If the diagnostic refutes the hypothesis, return to step 3 with the new
evidence. Do not skip ahead to a fix on a refuted hypothesis.

### 5. Stop the bleeding (if applicable)
If the system is actively burning users and the fix will take time:
mitigate first. Options, in order of preference:
- Roll back to the last known-good deploy.
- Feature-flag off the suspect path.
- Scale up if it's a capacity issue.
- Failover if it's regional.

Mitigation is not resolution. After mitigating, return to diagnosis.

### 6. Fix
Once the cause is confirmed (not just suspected):
- Apply the minimal change that resolves the cause.
- Test in staging if possible.
- Deploy with extra observation: tail logs, watch error rate, ready to
  roll back.

### 7. Verify
- The original symptom is gone (re-run the user-reported path, not just
  a health check).
- Error rate returned to baseline.
- No new errors introduced by the fix.

### 8. Learn (CoALA §4.6.4) — this is the part everyone skips
Append to episodic memory: the symptom, the false hypotheses, the actual
cause, the fix, the time-to-detect and time-to-resolve.

Reflect (`coala-reflection`): is there a stable semantic fact to extract?
A monitoring gap to fill? A skill to author for next time? An automated
check that would have caught this earlier?

## Pitfalls

- **Hypothesis-first debugging.** Reaching for a guess before reading
  logs. Causes you to fix imaginary problems while the real one runs.
- **Fix-and-pray.** Deploying a "probably this" fix without confirming
  the cause. If it works, you'll never know why — and the bug will
  return.
- **Forgetting the deploy log.** The most likely cause of "it broke now"
  is "we changed it now." Check recent deploys before exotic theories.
- **Mitigation as resolution.** Rolling back stops the bleeding but
  doesn't tell you what broke. Diagnose post-rollback.
- **Silent learning.** Fixing an incident without writing it down means
  the next person (or next-you) does the whole loop again.
- **Heroic one-person fixes during outage.** If it's bad, page another
  human. Cognitive load during incidents is the real bottleneck.

## Verification

- The reported symptom is reproducibly gone.
- The fix is traceable to a confirmed cause, not a guess.
- An episodic note exists with: symptom, cause, fix, prevention.
- Monitoring/alerting was updated if the incident wasn't detected fast
  enough.
