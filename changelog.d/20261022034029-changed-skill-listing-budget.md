- **Sessions on a 1M-token context window list every skill with its
  description again.** Claude Code gives the always-on skill listing 1% of
  the context window by default; a full Genesis install needs more, so the
  lower-priority skills were listed by name only, without the description
  that tells a session when to use them. The repo now sets
  `skillListingBudgetFraction` to 0.02. The budget is a ceiling, so an install
  with fewer skills pays nothing extra. A model with a 200k-token window still
  gets too small a budget for a full install.
