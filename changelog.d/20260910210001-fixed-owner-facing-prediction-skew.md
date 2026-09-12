- **Owner-facing messages no longer skew outreach reply-rate calibration.** Every
  message Genesis sends to *you* over Telegram or voice (approvals, digests,
  blockers, alerts, GitHub-activity pings, career nudges, marketing reply-pings,
  career "bite" alerts) was silently creating reply-received / positive-engagement
  predictions in the calibration ledger. Those go to you, not to an external
  recipient, so they can never get a reply — the predictions always resolved as
  "no reply", quietly dragging Genesis's measured outreach reply rate toward zero.
  The prediction hook now skips the owner-facing channels (Telegram / voice), so
  calibration reflects only genuine external outreach. Discriminating on channel
  (not category) is deliberate: a cold-marketing prospect email carries the same
  `notification` category as an owner ping but goes to `email` — so it stays
  correctly calibrated, while the owner ping is skipped. A one-time migration
  also **voids the historical** owner-facing predictions already in the
  ledger, so the existing contamination clears at the next grading recompute
  instead of lingering in the all-time reply-rate cell.
