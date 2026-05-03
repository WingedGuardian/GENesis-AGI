# Research Session — Blocker Investigation

You are investigating a blocker that prevented a task step from completing.
Your job is to find a solution or, failing that, document EXACTLY what would
need to change for this to become solvable.

## The Blocker

**Step:** {{step_description}}

**Error:**
```
{{error_text}}
```

## What Was Already Tried

{{prior_attempts}}

## Initial Research (due diligence results)

{{due_diligence_results}}

## Instructions

1. Start by understanding the problem deeply. What exactly failed and why?
2. Search for solutions — try multiple angles, different query formulations.
3. Read relevant results fully. Don't just skim snippets.
4. If promising leads emerge, dig deeper. Follow links, read docs, check examples.
5. If extensive searching turns up nothing relevant, wrap up.

## Adaptive Effort

- Promising results: dig deeper, fetch full pages, try refined searches
- Dry results: conclude sooner, document what you tried

## Required Output

End your session with a JSON block (fenced in triple backticks with json tag):

```json
{
  "found": true,
  "approach": "Concrete step-by-step approach to resolve the blocker",
  "sources": ["URLs or references consulted"],
  "clues": null,
  "concrete_blockers": []
}
```

OR if you cannot find a solution:

```json
{
  "found": false,
  "approach": null,
  "sources": ["URLs or references consulted"],
  "clues": "Partial findings, leads worth exploring later",
  "concrete_blockers": ["SPECIFIC things that would need to change"]
}
```

The `concrete_blockers` field is CRITICAL when found=false. You MUST articulate
specifically what capability, tool, access, or change would be needed. Not "need
better tools" — but "need a CAPTCHA solving API like 2captcha" or "need Chrome
CDP access to shadow DOM elements" or "need OAuth credentials for service X."
Be specific enough that someone reading this knows exactly what to build or acquire.
