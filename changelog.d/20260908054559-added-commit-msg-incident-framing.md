- **The commit-message hook now warns about security-incident framing.** Public
  artifacts should describe what the code does, not assert in past tense that this
  system leaked or exposed something — a fix does not need a confessed incident to
  justify it. The hook already blocked private addresses and install identifiers in
  commit messages; it now also prints a warning (never a block) when a message's
  SUBJECT reads that way, at the point where rewriting is still free. This matters
  most for commit messages specifically: squash merges take their text from the
  branch commits, so that wording is what lands permanently, and a pushed public
  branch cannot be force-pushed to correct it. Scope and wording are measured
  against this repository's own history, and the test suite re-derives both the
  hit rate and the recall on every run so a later edit cannot quietly narrow it.
