- **A daily watcher that announces once when the public repo crosses a star
  milestone**, so work parked behind "revisit at N stars" wakes up on its own
  instead of waiting to be remembered. It reads the count unauthenticated, writes
  a permanent observation on the transition, and stays silent otherwise; each
  configured milestone is announced once, including one added below a milestone
  already announced. An install with no repo configured falls back to the
  checkout's remote for the public repo, matched by owner/name (then `origin`);
  when several remotes carry repositories with the configured name and no owner
  is set, it does not guess and logs which key to set, and an install with no
  candidate at all is a clean no-op rather than a daily failing unit. Its state
  lives under `GENESIS_HOME`, and it never writes to a database the integrity
  check has quarantined.
