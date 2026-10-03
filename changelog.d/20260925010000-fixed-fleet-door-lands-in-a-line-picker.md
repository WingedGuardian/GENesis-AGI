- The SSH fleet door no longer drops you into an unrelated session on opening.
  A terminal that answers a DECRQM mode query replies with bytes like
  `ESC [ ? 2004 ; 2 $ y`, and tmux's client key parser delivers them as
  KEYSTROKES — it consumes Device-Attributes replies, which end in `c`, but not
  these, which end in `y`. The door landed straight in `choose-tree`, where `?`
  opens the search prompt (the yellow bar) and a bare digit chooses that entry.
  One stray reply therefore both painted the status line and took the
  selection, deterministically the same one, and the keypress meant to dismiss
  the bar confirmed it.

  The landing screen is now a line-based menu with no single-key actions, so
  stray bytes join a line that fails to parse and the menu redraws. `t` opens
  the session tree, `r` refreshes, `q` drops to a shell, and `Ctrl-b s` opens
  the tree from anywhere as before.

  Ruled out by measurement rather than argument: draining the tty before
  attaching (still stolen), hosting the chooser in a pane (prompt gone, still
  stolen), any recovery keypress (already switched), and upgrading tmux — 3.7c
  was built from source and re-probed and is still stolen, and a pane's query
  never reaches the terminal on 3.4 either, so an upgrade removes no source.

- The fleet door now degrades to a login shell when its picker is missing or
  not executable, and says why. Previously the pane command failed, which
  destroyed the window, then the session, then the connection — so a missing
  file logged the operator out with `[exited]` and no explanation, on every
  connect.

- The generated `~/.ssh/config` block no longer describes the door as opening
  `choose-tree`, or `Ctrl-b s` as reopening "the picker". Both were stale, and
  that text ships to client machines.
