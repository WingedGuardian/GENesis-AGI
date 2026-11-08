## Which session are you? Load that skill now

This is the shared core. Each session type has its own skill: load it with the
Skill tool, or Read its file if this text reached you by injection.

- **Spec**, shaping a change before anyone plans it: `spec-session`
  (`.claude/skills/spec-session/SKILL.md`).
- **Build**, one item from Ready to its open PR, including rework from a
  send-back spec: `build-session` (`.claude/skills/build-session/SKILL.md`).
- **Close**, driving open PRs to merge and fixing their review findings:
  `closing-session` (`.claude/skills/closing-session/SKILL.md`).
- **Validate**, post-merge verification, deploy and E2E: `validating-merges`
  (`.claude/skills/validating-merges/SKILL.md`).

Decide from the owner's words, the task's shape, or the dispatcher's prompt;
none fits (debugging the runtime, a one-off fix)? Load `build-session`. Two
types (spec, then build) load both, in order. Build and close sessions Read
`.claude/skills/genesis-development/references/commit-path.md` before their
first commit.
