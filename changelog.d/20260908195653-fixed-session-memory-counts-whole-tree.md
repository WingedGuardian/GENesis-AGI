- **Per-session memory on the dashboard now counts the whole session, not just
  one process of it.** A Claude Code session is not a single program: alongside
  the main process it runs a language server and a fleet of MCP servers, and
  those children turn out to be about two thirds of what a session actually
  costs. Only the main process was being measured, so seven concurrent sessions
  reported roughly 5 GB when the real figure was closer to 16 GB.

  The more consequential half was invisible: the warning and critical
  thresholds were applied to that same partial number. A session whose true
  footprint had grown past the alert threshold still showed a small main
  process and reported healthy, so the alert could not fire in the situation it
  was written for. Session memory is now measured across the whole process
  tree, the thresholds are rebased on what a healthy session actually costs,
  and the alert says how much sits in the main process versus its children —
  so the figure can be reconciled against what you would see in `top`.
