**Load `genesis-development` (the shared core) first** if this session has not:
it holds the review-chain, evidence and private-data rules every session uses.

**Mandatory read before your first commit:**
`.claude/skills/genesis-development/references/commit-path.md`. A closing
session commits review fixes, and the commit gates bind it the same way.
Fixing a finding also uses rules that live in
`.claude/skills/build-session/SKILL.md`: "Instance-Fix vs Class-Fix Gate",
"Debugging Discipline", "Test-First Discipline, continued", "Verify Outcomes,
Not Just Tests", "Advisory is the default; a BLOCK is what needs the
argument", "Guard failure semantics", "Common Traps" (the canonical-parser
mandate and the guard corollaries), "CI checks" and "A blocked compound command
loses EVERYTHING in it". Read the one a finding touches.
