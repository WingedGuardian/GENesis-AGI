---
name: retrospective
description: Post-interaction retrospective analysis — use after completing a significant task, conversation, or phase to extract lessons, identify process improvements, and update procedures
consumer: cc_background_reflection
phase: 6
skill_type: workflow
---

# Retrospective

## Purpose

Analyze a completed interaction or task to extract lessons, identify process
improvements, and update procedural memory.

## When to Use

- After a multi-step task completes (success or failure).
- After a user interaction that revealed a gap or surprise.
- After an obstacle was resolved (capture the resolution pattern).
- Scheduled: end of each active work session.

## Workflow

1. **Reconstruct timeline** — What happened, in what order? What was the goal?
2. **Identify outcomes** — Did it succeed? Partially? What was the quality?
3. **Extract surprises** — What was unexpected? What assumptions broke?
4. **Find patterns** — Does this match any existing procedural knowledge?
   Does it contradict any?
5. **Derive lessons** — Concrete, actionable learnings (not vague platitudes).
6. **Update memory** — Write observations, update procedures if warranted,
   flag contradictions for user review.

## Output Format

```yaml
subject: <what was analyzed>
date: <YYYY-MM-DD>
outcome: success | partial | failure
surprises:
  - <unexpected finding>
lessons:
  - <concrete actionable lesson>
procedure_updates:
  - procedure: <name>
    change: <what to update>
observations:
  - <observation to store>
```

## References

- `src/genesis/learning/procedural/` — Procedure CRUD for updates
- `src/genesis/learning/observation_writer.py` — Writing observations
