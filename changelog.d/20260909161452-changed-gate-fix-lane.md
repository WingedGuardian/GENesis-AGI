- **Changing the review gate no longer has to pass the gate it is changing.** A PR
  touching the enforcement-hook surface now runs an expedited lane: both cross-model
  reviewers on round 1 instead of one, findings triaged before any code changes, a
  single batched fix push, and a hard stop at two rounds. It is a stricter-review,
  fewer-rounds trade — the always-fix floor (P1, security, destructive, fail-open) is
  never waived, and a new P1 in the fix itself stops for a human decision. The lane is
  process doctrine layered under the existing round caps, so nothing about the gate's
  code changed. Which secondary reviewer an install uses is now named once in
  `merge_gate.secondary_reviewer` in local config, rather than written into the repo
  where it goes stale; installs without one simply run Codex-only.
