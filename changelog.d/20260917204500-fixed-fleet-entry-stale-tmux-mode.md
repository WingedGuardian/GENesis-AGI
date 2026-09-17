- Connecting to the SSH fleet no longer drops you into a "frozen" session with a
  yellow highlighted row. That pane was sitting in a tmux mode (a `choose-tree`
  picker or `copy-mode`) left behind by an earlier disconnect — a mode belongs
  to the pane, not the client, so it survived indefinitely and the next
  connection that selected that slot landed straight back inside it. Both
  doors now clear such panes on the way in, without disturbing the pane's
  process or scrollback, and only when no client is attached — a session you
  are actively looking at is never touched. Each entry is recorded to
  `~/.genesis/logs/fleet_entry_<date>.log` (owner-only, pruned after 45 days);
  set `GENESIS_FLEET_GUARD_CLEAR=off` to keep the logging without the clearing.
