- **The temp watchdog can now reclaim abandoned pytest directories.** Every
  `/tmp` sweep skipped anything named `pytest-*`, to avoid deleting files out
  from under a running test suite — but the exclusion could not tell a live run
  from a finished one, so it protected the leftovers just as carefully. On one
  install that left 255 MB, half of a 512 MB RAM-backed `/tmp`, untouchable
  while the emergency tier ran every 30 seconds reporting success. Pytest
  directories are now handled whole and judged by liveness instead of by name:
  a tree is kept if a process still holds a file open inside it, if the pid
  recorded in pytest's own lock file is still running, or if it is newer than
  the tier's age threshold. The generic sweeps keep their exclusion, so nothing
  ever deletes individual files out of a live suite's directory, and a tree
  that cannot be fully removed is now reported rather than retried in silence.
