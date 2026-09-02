# Agentic Process Lessons — External Evidence Worth Keeping

Distilled from Cole Medin's published methodology and shipped artifacts
(github.com/coleam00 — skills, Archon `sdlc` pack, context-engineering-intro,
ai-transformation-workshop; his "11 tips" video, 2026-08), cross-checked
against independent measurements where they exist. Kept here because these
lessons generalize past any one pipeline: they apply to how Genesis sessions
work, tiered process or not.

Status: reference document. Mechanisms Genesis has adopted are cited in the
work-tracking and dev-process design docs; this file is the durable index of
the *lessons*, with the numbers that back them.

## The through-line

> **The context window is working memory, not storage.**
> A rule that must always hold → put it in a hook.
> Something the agent worked out → write it to disk.
> Everything else in there → assume it is gone.

Everything below is this, applied.

## The Five Golden Rules (verbatim)

1. **Commandify everything.** Typed more than twice → a command. Reusable,
   shareable, evolvable.
2. **Reduce assumptions.** Questions before PRD; review the PRD before
   tickets; review the plan before executing. The most dangerous thing is not
   the model making mistakes — it is the model making assumptions. Every
   question it asks is an assumption it is not making.
3. **Context is king.** Reset between planning and implementation. Sub-agents
   for research only. On-demand context over a bloated rules file.
4. **Git log is memory.** Commit frequently, descriptively — the agent reads
   history on every prime.
5. **System evolution.** Every bug the agent makes is a chance to improve the
   AI layer so it never makes that mistake again. The system compounds.

## The validation pyramid

```
Layer 5: Manual testing            ← human (golden path + edge cases)
Layer 4: Code review               ← human (with AI assist)
Layer 3: Integration / E2E         ← agent + browser automation, iterates
Layer 2: Unit tests                ← agent handles, iterates
Layer 1: Type checking + linting   ← agent handles, iterates
```

Goal: push the line between layers 3 and 4 as far down as possible. Every
gate declares which side of the line it sits on — no gate is ambiently
"reviewed."

## The 11 tips, with their numbers

1. **Write for the agent, not the human.** File paths, exact commands,
   numbers — "all SQL lives in the database folder," never "keep database
   code organized sensibly."
2. **Instruction files rot.** ~1 in 4 studied repos with AI rules carry
   stale ones. Wrong rules are worse than missing rules; audit for drift.
3. **Auto-compaction is not worth trusting.** ~10% of specific detail
   survives the summary. Smaller units of work; manual handoff docs + fresh
   sessions over `/compact`.
4. **Load-bearing rules go in hooks.** Rules are probabilistic; hooks are
   deterministic. Anything that must happen every single time is a hook.
5. **Less context beats more** — but zero is also wrong (projects with no AI
   config measurably grow more complex). Rules files: a few hundred lines,
   project-specific constraints only; everything else on-demand.
6. **Parallel agents cost more than you think.** One practitioner measured
   ~39% of a week's usage burned while 4+ sessions ran concurrently; an
   agent team costs roughly 7× one session. Fan out deliberately.
7. **Never escalate mid-task.** A degrading trajectory is poisoned context,
   not an underpowered model — a bigger model recovers less than half the
   gap. Write a handoff, burn the session, start fresh.
8. **Coordinators are theater.** Agent-to-agent meshes and team-lead layers
   buy measurable coordination overhead over one well-prompted delegator.
9. **Never let the writer approve the work.** The authoritative review runs
   in a NEW conversation over a concrete artifact (a diff, a PR). Intent is
   not evidence; the diff is.
10. **It is possible to over-revise.** In forced 10–20-iteration runs, ~85%
    of the time an earlier iteration beat the final one. Bound iteration;
    compare intermediate states; don't spend leftover budget on "make it
    perfect."
11. **Validation is a system, not a step.** Define the whole harness — tools,
    test conventions, edge-case strategy — before writing any code.

## The mechanisms that enforce the lessons (from shipped code, not prose)

- **A stage passes by evidence, not exit code.** An AI node that declines
  still exits 0; check deterministically for the artifact (tree diff since a
  recorded start-SHA, commit delta, or an honest evidenced decline).
- **Blocked is a valid completion.** `ready:false` naming exactly what is
  missing beats a plan built on guesses.
- **Classify every red**: `introduced | inherited | environment`, evidence
  required — "treating red as one thing killed two correct deliveries."
- **Positive assertions only.** No gate tests for the absence of "error"; a
  check that silently never ran must read as failure, not success.
- **Independence is checked, not assumed.** A reviewer whose inputs contain
  the builder's plan quietly starts agreeing with the builder — check for
  artifact leakage; "an independence property nobody checks is one nobody
  has."
- **Uncalibrated thresholds cap autonomy.** A numeric gate whose threshold
  was never calibrated refuses to auto-proceed and hands the decision to a
  human — it never invents green.
- **A gate that cannot fail is a comment.** Every guard ships a fixture that
  makes it fire and a control proving it doesn't over-block.
- **"YAML coordinates. Code computes. Agents judge."** Config stays
  declarative; computation lives in scripts; judgment stays with the model —
  and config grows expression syntax only over someone's objection.

## Sources

Primary: the repos and video named in the header, fetched and read as source.
Independent corroboration of tips 3/5/11: a 47-run empirical test of the same
claims (sharp stop conditions 31% faster / 7% cheaper than vague prompts;
reusable rules cut cost ~21.6%) — the mechanisms replicate even where the
surrounding terminology is marketing.
