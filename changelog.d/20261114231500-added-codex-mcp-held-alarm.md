- **The Sentinel proposes the Codex MCP stop before memory runs out.** A new
  health alarm, `infra:codex_mcp_held`, fires when MCP servers held by a shared
  Codex app-server reach `codex_mcp_stop.propose_at_mb` (default 4096 MB) by the
  stop tool's own scan, and wakes the Sentinel to propose `codex_mcp_stop`; the
  owner still approves every stop. Before this, the proposal waited for the
  container-memory alarm at 85%, when capped jobs were already refused. A
  dispatcher rule comes with it: a confirmed, stronger alarm (a higher tier, or
  a CRITICAL of the same tier) takes over an approval park held only by WARNING
  alarms, so an unanswered early proposal never blocks an outage. A declined
  stop is quiet for 24 h, as every declined Sentinel request already is.
- **`codex_mcp_stop` now finds the Genesis MCP servers it was built to stop.**
  It identified an MCP server by pipes on stdin and stdout, but the MCP Python
  SDK moves the protocol stream to private descriptors and points stdin at
  /dev/null, so every Genesis Python server was skipped (on a live daemon, 5 of
  22 groups and about 1,200 of 3,800 MB were found). A target must now read a
  pipe the app-server writes and write one it reads, on any descriptor, and no
  other child of the app-server may hold either pipe. A chat command Codex
  starts in pipe mode, whose stdin is /dev/null, still never qualifies.
- **A take-over never drops the owner's answer.** If cancelling the parked
  request fails because it is no longer pending, the park is kept; and a
  take-over happens only when the alarm the dispatch would lead with is the
  challenger and is itself clear of backoff.
