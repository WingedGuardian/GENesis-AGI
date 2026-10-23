---
name: spec-session
description: >
  This skill should be used when a session SPECIFIES a Genesis change before it
  is planned or built: shaping the problem, the PRD, the architecture and the
  tickets a build session will take. Load `genesis-development` (the shared core)
  first. Do NOT load it for building (`build-session`), closing open PRs
  (`closing-session`), or post-merge verification (`validating-merges`).
# MEASURED against the real scorer on the live catalog, 2026-10-21: 4/4
# advertised spec prompts fire this skill, 0/10 control prompts do.
keywords: [spec, specs, prd]
consumer: cc_foreground
phase: 10
skill_type: workflow
---

# Spec session

**Load `genesis-development` (the shared core) first** if this session has not:
it holds the review-chain, evidence and private-data rules every session uses.

A minimal skill for now; its process is designed later. Also read:
- the core's "Due diligence before building" and "Architecture Review"
  (`.claude/skills/genesis-development/SKILL.md`);
- `.claude/skills/genesis-development/references/due-diligence.md` and
  `.claude/skills/genesis-development/references/plan-docs.md`;
- `.claude/docs/premise-check.md`, and the `evaluate` skill for any external
  candidate;
- `build-session`'s "Generalizability Gate" before a spec names where code runs.
