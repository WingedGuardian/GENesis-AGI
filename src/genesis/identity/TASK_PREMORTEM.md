# Genesis --- Task Pre-Mortem Analysis

You are reviewing a task plan. **Assume the task FAILS COMPLETELY.**

Your job: identify what would cause failure BEFORE resources are committed.
This is not a completeness check — it is a correctness and feasibility check.

## The Plan

{{plan_content}}

## Task Description

{{task_description}}

## Instructions

1. What are the 3 most likely causes of complete failure?
   Focus on fundamental approach issues, not implementation details.

2. What single assumption in this plan, if wrong, would invalidate the
   entire approach? (Not "the code might have a bug" — more like
   "this assumes the API supports batch operations, but it might not.")

3. What concrete mitigations would you add to the plan to address the
   top risks? Be specific and actionable.

4. Confidence (0-100) that this plan succeeds as written, without
   modifications. Be honest — most plans have gaps.

## Output Format

Return ONLY a JSON object:

```json
{
  "failure_modes": [
    "Most likely cause of failure",
    "Second most likely",
    "Third most likely"
  ],
  "invalid_assumptions": [
    "The critical assumption that could invalidate the approach"
  ],
  "mitigations": [
    "Specific mitigation 1",
    "Specific mitigation 2"
  ],
  "confidence": 72
}
```

## Judgment Guidelines

- A confidence of 100 means "this plan cannot fail" — almost never true.
- A confidence below 50 means "this plan is more likely to fail than succeed."
- Focus on structural risks, not cosmetic issues.
- If the plan is straightforward and well-scoped, say so — don't manufacture
  risks to seem thorough.
