- **Sessions started at a checkout's root list every skill with its
  description again** (on a 1M-token context window, while the catalog fits
  60,000 chars). Claude Code gives the always-on skill listing 1% of the
  context window by default; a large catalog (122 skills on one measured
  install) needs more, so the lower-priority skills were listed by name only,
  without the description that tells a session when to use them. The repo now
  sets `skillListingBudgetFraction` to 0.02. The budget is a ceiling, so an
  install with fewer skills pays nothing extra. Sessions started elsewhere,
  including most dispatched sessions, keep Claude Code's default, and a model
  with a 200k-token window still gets too small a budget for a full install.
