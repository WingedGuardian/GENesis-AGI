- The fleet door no longer makes tmux log an error on every connection. It
  checked whether a session name was free with `has-session`, and on tmux "no
  such session" is an *error*, recorded in the server's message log. Because
  the check runs on the success path — the name is always free — one entry
  accumulated per connection. The door now tests for the session without making
  tmux raise an error.

  This fragment previously claimed the fix also stopped a yellow bar flashing
  across the window and dropping you into an unrelated session. It did not, and
  the mechanism it gave was wrong: a tmux error is not painted on attached
  clients' status lines from a door invoked as an ssh `RemoteCommand`, where
  the issuing client has no session and the error goes to its own stderr.
  MEASURED on tmux 3.4 — three absent probes added three log entries and zero
  bytes to an attached client's stream, while a `display-message` control on
  that client did paint. The yellow bar had a different cause and is fixed
  separately.
