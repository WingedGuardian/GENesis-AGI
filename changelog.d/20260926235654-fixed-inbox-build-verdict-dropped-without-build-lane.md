- **Inbox capability-build verdicts are no longer silently dropped when the
  build lane is off.** A `BUILD` recommendation is deliberately skipped by
  follow-up creation because the build lane owns it — but the lane is
  optional, and when it is unwired or disabled it ignores the verdict too, so
  the evaluation reached no consumer at all. With the lane not live, the
  verdict is now recorded by what it says: `build` becomes a pinned,
  user-owned follow-up; `needs_discussion` becomes a pinned follow-up labelled
  as a discussion rather than a build task; a `dont_build` veto is kept as a
  tabled record and never becomes actionable work. Each carries the verdict's
  stated reason. As in the lane, a `build` verdict whose build spec is missing
  or incomplete is recorded as `needs_discussion`, and a BUILD block with no
  valid verdict creates nothing and logs a warning. Rows are deduplicated per
  item and verdict: a re-evaluation that merely rephrases the next step adds
  nothing, while a changed verdict is recorded. With the lane live, nothing
  changes. If you enable the lane later, an item's still-pending follow-up is
  closed with a note once the lane takes that item over (when the item is next
  re-evaluated), because the lane's greenlight card replaces it; and an item
  the lane already holds gets no follow-up if the lane is later disabled.
