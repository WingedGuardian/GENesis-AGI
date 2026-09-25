- The slot door no longer adds three entries to tmux's server message log on
  every login. `cc-slot.sh` tested for a session with `tmux has-session`, which
  answers "absent" by ERRORING, and all three probes asked on paths where
  absent is the ANSWER rather than a fault: the free-slot search exits by
  failing, so allocating any slot logged an error, and the two pre-create
  checks logged one each on every first connection to a slot. Replaced with a
  silent `_session_exists` helper that answers from tmux's own `-f` filter.

  This also corrects the mechanism stated in the previous fleet-door fix, which
  claimed a tmux error is "painted on every attached client's status line" and
  that redirecting stderr therefore did nothing. MEASURED on tmux 3.4 and
  refuted: with a client attached through a pty, three absent probes added
  three log entries and zero bytes to that client's stream, while a
  `display-message` control on the same client did paint. For an ssh
  RemoteCommand door the issuing client has no session, so the error goes to
  that client's own stderr, which the redirect did suppress. The yellow bar
  operators actually saw came from a different mechanism entirely — a terminal
  reply delivered as keystrokes into `choose-tree` — and is fixed separately.
