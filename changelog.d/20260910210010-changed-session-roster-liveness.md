- **Concurrent-session awareness now tells live from gone — and idle from
  dead.** Each session's heartbeat now records which OS process it belongs to,
  its working directory and git branch; every reader derives liveness fresh
  from the process table instead of trusting row age. The per-prompt
  concurrent-sessions line shows it: `live` / `live-no-sock` / `gone`, the
  branch and directory a peer is working in, and how long it has been idle —
  an idle session in an open terminal now stays visible instead of vanishing
  after ten minutes, and a crashed one is marked instead of lingering. The
  line is also bounded (eight peers plus an overflow count) so a busy machine
  cannot flood a session's context. Rows written before the upgrade render
  honestly as identity-unknown.
