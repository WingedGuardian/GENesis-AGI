- **Ambient background workers no longer clutter the session-resume picker.**
  The shared headless runner (memory arbiter, session-ledger extractor,
  repo-pulse matcher) spawned its one-turn `claude -p` calls from the server's
  own working directory, so their transcripts accumulated under the interactive
  project and surfaced in Claude Code's `--resume` list — previewing as
  injected hook text, since they contain no human prompt. The runner now uses
  a fresh working directory per call, outside any repository, removed again
  afterwards. Nothing can pre-place instructions — or a hook-bearing project
  config, which would execute — in a directory whose name did not exist a
  moment ago, which removes the working-directory surface a long-lived shared
  directory exposed. Directories ABOVE it are a separate matter and are not
  addressed here: Claude Code reads memory files from a working directory's
  ancestors as well. This also keeps the resume picker clean, stops the
  project's session-start hooks from injecting kilobytes of context into
  calls that cannot use it, and denies the judges every built-in tool
  outright.
