- **A session slot that lost its Claude Code no longer traps you at a bare
  prompt — the door offers to rebuild it, and asks first.** A slot could exist
  as a plain shell (its claude exited, or it was created bare), and connecting
  to it silently attached to that shell instead of launching anything: the
  connection machinery discards its launch command when the session already
  exists, so every entry landed at the same dead prompt. On one install that
  went on for three weeks.

  Connecting to such a slot now says plainly what was found, and — only at a
  real terminal, only on an explicit yes, with No as the default — ends the
  bare session so the normal creation path rebuilds it with its full
  environment. Consent is for the state you were shown, not for the slot: if
  anything changed while you decided (something started in that pane, another
  connection got there first, even the whole server restarting underneath),
  the door stands down and attaches instead. Without a terminal to ask, it
  only reports, and names the manual route. The slot listing marks such
  sessions too, pointing at the door that can now actually fix them.
