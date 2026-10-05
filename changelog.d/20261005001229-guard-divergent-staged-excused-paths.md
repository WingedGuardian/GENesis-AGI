- **Refuse unsafe staged install-local path changes before an update stops services.**
  An update now refuses when divergent history includes a staged addition or
  deletion on an excused install-local path. Staged edits, fast-forward updates,
  and local-ahead updates remain allowed.
