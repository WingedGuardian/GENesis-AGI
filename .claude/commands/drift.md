Read the Genesis build phases document:
  docs/architecture/genesis-v3-build-phases.md

Read the git log for the last 60 days:
  git log --oneline --since="60 days ago"

Read the current directory structure (excluding docs/).

For each build phase:
1. What does the plan say should be implemented?
2. What evidence exists in the codebase that it IS implemented?
3. What is missing?
4. What exists in the code that is NOT in any plan? (scope creep or discovery?)

Output a structured drift report:
- IMPLEMENTED: [phase] — [component] — [evidence: file/commit]
- MISSING: [phase] — [component] — [no evidence found]
- UNPLANNED: [component] — [exists but not in any phase]
- DRIFT SCORE: X% of planned items implemented
