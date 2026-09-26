- **Inbox capability-build verdicts are no longer silently dropped when the
  build lane is off.** A `BUILD` recommendation is deliberately skipped by
  follow-up creation because the build lane owns it — but the lane is
  optional, and when it is unwired or disabled it ignores the verdict too, so
  the evaluation reached no consumer at all. Follow-up creation now checks
  whether the build lane is live: when it is not, the verdict becomes a
  pinned, user-owned follow-up (medium priority) carrying the verdict and its
  stated reason; when it is, nothing changes and no duplicate is created.
