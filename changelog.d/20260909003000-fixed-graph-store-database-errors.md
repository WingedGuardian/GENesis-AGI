- **A database problem no longer surfaces as a raw error from a memory
  recall.** The in-process graph store answers by reading the memory database,
  and if that read failed — a locked file, a connection that had been closed
  underneath it — the underlying error escaped instead of being reported as
  "the graph is unavailable". That distinction is what the fallback chain keys
  on, so the failure skipped it entirely: no fallback to the SQL path, no
  warning in the log, just the raw error arriving wherever the traversal had
  been called from.

  Both halves are now reported as unavailability, which is what the surrounding
  code was already written to handle. The second half matters because the SQL
  fallback reads the same database connection that just failed — so for anything
  other than a passing lock it fails the same way, and fixing only the first
  half would have moved the raw error rather than removing it.
