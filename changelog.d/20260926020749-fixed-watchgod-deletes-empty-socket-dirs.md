- **The cc-tmp watchdog no longer deletes Claude Code's empty socket
  directories.** Its sweeps already refused to delete a unix socket, but a
  socket is a file and that guard could not protect the *directory* holding
  it — a sockets directory survived only while something inside kept it
  non-empty, which is protection by accident. Reproduced at both deletion
  sites: an empty sockets directory and an empty daemon root were removed
  while a socket-holding sibling survived. Both sites now spare those
  directories and any directory beneath them, while still reclaiming regular
  files inside them and still reaping unrelated empty directories.
  The exclusions are **exact paths**, not name patterns, and both halves of
  that matter. A `cc-socks*` pattern would make an unbounded subtree immortal
  (a directory merely *starting* with that name would pin everything under it
  against the watchdog forever), and `cc-daemon-*` is worse than wide — Claude
  Code also uses that string as a `mkdtemp` prefix for throwaway directories
  holding a single log file, so a pattern would empty each one and then spare
  the husk permanently, turning the empty-directory reaper into an
  empty-directory leaker. The paths are enumerated rather than computed from a
  single expression, because the watchdog and a Claude Code session do not
  share an environment: measured on a live install, the watchdog runs with
  `XDG_RUNTIME_DIR` set by its service manager while a session under a
  terminal multiplexer has it unset and falls back to its temp dir, so both
  locations existed at once and a default-expansion evaluated in the watchdog
  would have stopped sparing the one inside the budget it sweeps.
  Severity, stated precisely: Claude Code recreates a missing sockets
  directory on demand, so this is not a lasting outage. What it costs is a
  lost race — a sweep landing between the path check and the directory's
  creation makes that session refuse to bind, and it runs without
  cross-session messaging for its lifetime rather than retrying.
  The exclusions are also escaped and normalised before they reach the search
  tool, which takes a *pattern* rather than a literal path. The temp directory
  is configurable, so a bracket, star or question mark in it — or a trailing
  slash — used to leave the exclusion matching nothing at all, deleting the
  very directory it was written to spare. The same omission was found on the
  exclusion protecting the active session's own files, and that one needs no
  configuration change at all to reach: the project directory name comes from
  the repository path, so a bracket in a repository name was enough to make
  the emergency sweep delete the running session's working files. Reproduced
  in every shape against a plain-path control that kept them. Path
  normalisation now happens where the configured value enters rather than at
  each place it is compared, because normalising one side of a comparison
  moves the mismatch instead of closing it.
