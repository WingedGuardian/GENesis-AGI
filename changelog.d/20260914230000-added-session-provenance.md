- **A new command answers "which session produced this?" — and the reverse — from
  git history alone.** Given a file, a line, or a commit, it reports the session
  that made it; given a session, it reports what that session changed. It reads
  only what is already recorded in the repository, so it works retroactively on
  history nobody instrumented at the time.
- It is a tool you run by hand. Nothing calls it automatically and nothing
  depends on it, so it changes no existing behaviour.
