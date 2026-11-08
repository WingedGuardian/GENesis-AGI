---
name: build-session
description: >
  This skill should be used when a session BUILDS one Genesis item: implementing
  a change in a worktree through to opening its PR, including rework from a
  send-back spec. Load `genesis-development` (the shared core) first. Do NOT load
  it for driving open PRs to merge (`closing-session`), post-merge verification
  (`validating-merges`), or specifying a change before it is planned
  (`spec-session`).
# MEASURED against the real scorer (scripts/hooks/skill_injection_hook.py) on
# the live catalog, 2026-10-21: 3/6 advertised build prompts fire this skill,
# and 2/10 control prompts do ("implement a grocery budget", "rework my
# resume"): both words are generic. The router in genesis-development, not this
# nudge, is what loads a type skill.
keywords: [implement, rework]
consumer: cc_foreground
phase: 10
skill_type: workflow
---

# Build session

**Load `genesis-development` (the shared core) first** if this session has not:
it holds the review-chain, evidence and private-data rules every session uses.

**Mandatory read before your first commit:**
`.claude/skills/genesis-development/references/commit-path.md`. It holds the
review_state sequence, the sigils, the Pre-Commit Gate, the depth gate and
verify-RED. Read it; it is not on-demand reference.
