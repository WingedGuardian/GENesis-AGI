# Genesis --- Task Execution Retrospective

You are analyzing a completed task execution trace to extract reusable
learnings. Your goal: identify patterns worth capturing so future similar
tasks execute better. This is how Genesis gets smarter over time.

## Execution Trace

{{trace_summary}}

## Existing Procedures

These procedures already exist in Genesis's memory. If the execution
confirms or contradicts any of them, note that in procedure_updates.

{{existing_procedures}}

## Instructions

Analyze the trace and extract:

1. **New procedures** --- reusable step sequences that worked (or failed in
   an instructive way). Only extract genuinely reusable patterns, not
   one-off task specifics. A good procedure answers: "If I see this kind
   of task again, what should I do differently or the same?"

2. **Procedure updates** --- if existing procedures were relevant and the
   execution confirms or contradicts them, note the outcome. This drives
   confidence calibration (Laplace smoothing) so good procedures rise and
   bad ones get demoted.

3. **Skill observations** --- if skills were used and could be improved
   (missing steps, edge cases, better instructions), note the delta. If
   a task revealed a reusable pattern and no matching skill exists, note
   that too.

## Output Format

Return ONLY a JSON object:

```json
{
  "new_procedures": [
    {
      "task_type": "kebab-case-identifier",
      "principle": "one sentence explaining why this procedure exists",
      "steps": ["step 1", "step 2"],
      "tools_used": ["tool1", "tool2"],
      "context_tags": ["tag1", "tag2"]
    }
  ],
  "procedure_updates": [
    {
      "task_type": "existing-procedure-task-type",
      "outcome": "success or failure",
      "failure_condition": "what went wrong (if failure)",
      "workaround": "what worked instead (if any)"
    }
  ],
  "skill_observations": [
    {
      "skill_name": "name from catalog",
      "observation": "what should be improved or added"
    }
  ]
}
```

## Judgment Guidelines

- Most routine tasks produce no learnings. Return empty arrays if nothing
  is genuinely worth extracting. One good procedure is worth more than
  five weak ones.
- Extract from both success AND failure. A clean success confirms a
  pattern. A failure-then-recovery reveals a workaround. A pure failure
  documents what not to do.
- Procedures should be specific enough to be actionable ("use --no-verify
  flag when X") not vague ("be careful with Y").
- Think about what would help if this exact task type came up again next
  week. What would you want to know?

## Handling Failures

If the trace shows failed steps:
- Document failure patterns as procedure_updates with `outcome: "failure"`
- Include `failure_condition` with the specific technical root cause
- Include `workaround` if a workaround was attempted (even if it failed)
- For ALL-failed traces: focus entirely on documenting what went wrong and why
- Never create a `new_procedure` from failure — only update existing ones
