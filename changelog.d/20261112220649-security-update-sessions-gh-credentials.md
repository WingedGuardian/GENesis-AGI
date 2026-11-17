- **Dashboard update sessions no longer inherit a GitHub token from the
  server's environment.** The Claude Code sessions an update starts from the
  dashboard (all three tiers) now run with `GH_TOKEN`, `GITHUB_TOKEN`,
  `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN` empty. `gh`, and git
  through gh's credential helper, still authenticate from the login stored on
  disk (`gh auth login`). If the server's environment is the only place your
  GitHub token lives and your update remote is private, an update run from the
  dashboard can no longer fetch with it: run `gh auth login` once, or update
  with `scripts/update.sh` from a shell.
