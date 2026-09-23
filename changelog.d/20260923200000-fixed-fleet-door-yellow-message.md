- Opening the SSH fleet no longer flashes a yellow bar across the bottom of the
  window and drop you into an unrelated session when you press a key to dismiss
  it. The lobby door checked whether a session name was free with tmux
  `has-session`, and on tmux "no such session" is an *error* — a server-side
  message painted on every attached client's status line in `message-style`
  (yellow). Because the check runs on the success path (the name is always
  free), it fired on every connection; the session picker was open underneath
  the whole time, so the keypress meant to dismiss the message was taken by the
  picker as "open the highlighted session". The door now tests for the session
  without making tmux raise an error.
