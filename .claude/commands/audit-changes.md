Review all changes in the current working tree (staged and unstaged).

For EACH modified file:
1. What was the original request that motivated this change?
2. Does the change match the request EXACTLY, or does it include extras?
3. What assumptions did you make that were not explicitly stated?
4. What edge cases exist that are not handled?
5. Does the code match the patterns in CLAUDE.md and the architecture docs?

Then holistically:
6. Are there files that SHOULD have been changed but were not?
7. Do any changes contradict each other?
8. Would a fresh reviewer find anything surprising?

Output a structured report. If any issues found, fix them before proceeding.
If no issues found, state "AUDIT CLEAN" explicitly.
