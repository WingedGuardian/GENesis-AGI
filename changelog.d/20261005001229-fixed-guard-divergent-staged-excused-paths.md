- **Refuse unsafe staged install-local path changes before an update stops services.**
  On divergent history, an update now refuses when an excused install-local path
  has a staged change the pre-merge clear cannot undo, including additions,
  deletions, intent-to-add entries, gitlinks, and any staged change on the
  de-tracked transitional paths. Staged edits to `AGENTS.md` and
  `config/procedure_triggers.yaml`, fast-forward updates, and local-ahead updates
  remain allowed. The guard runs again immediately before services stop.
