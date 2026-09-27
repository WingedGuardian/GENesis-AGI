- **Inbox capability-build verdicts are no longer silently dropped when the
  build lane is off.** A `BUILD` recommendation is deliberately skipped by
  follow-up creation because the build lane owns it — but the lane is
  optional, and when it is unwired or disabled it ignores the verdict too, so
  the evaluation reached no consumer at all. With the lane not live, the
  verdict is now recorded by what it says: `build` becomes a pinned,
  user-owned follow-up; `needs_discussion` becomes a pinned follow-up labelled
  as a discussion rather than a build task; a `dont_build` veto is kept as a
  tabled record and never becomes actionable work. Each carries the verdict's
  stated reason. A BUILD block with no valid verdict creates nothing and logs a
  warning, matching the lane. A changed verdict for the same item is recorded
  rather than deduplicated away. With the lane live, nothing changes.
